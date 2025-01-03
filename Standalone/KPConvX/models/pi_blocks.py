#
# For licensing see accompanying LICENSE file.
# Copyright (C) 2024 Apple Inc. All Rights Reserved.
#
# ----------------------------------------------------------------------------------------------------------------------
#
#   Hugues THOMAS - 06/10/2023
#
#   KPConvX project: pi_blocks.py
#       > Legacy developments for point cloud involution blocks
#
#
#
#      0=================================0
#      |    Kernel Point Convolutions    |
#      0=================================0
#
#
# ----------------------------------------------------------------------------------------------------------------------
#
#      Define network blocks
#
# ----------------------------------------------------------------------------------------------------------------------
#
#      Hugues THOMAS - 06/03/2020
#

import math
import torch
import torch.nn as nn
from torch import Tensor
from torch.nn.init import kaiming_uniform_
from typing import Callable
from collections import OrderedDict



from kernels.kernel_points import load_kernels
from models.generic_blocks import index_select, radius_gaussian, local_maxpool, UnaryBlock, BatchNormBlock, GroupNormBlock, NormBlock

# ----------------------------------------------------------------------------------------------------------------------
#
#           Simple functions
#       \**********************/
#



# ----------------------------------------------------------------------------------------------------------------------
#
#           KPInv class
#       \*****************/
#

class point_involution_v1(nn.Module):
    

    def __init__(self,
                 channels: int,
                 neighborhood_size: int,
                 radius: float,
                 groups: int = 8,
                 channels_per_group: int = -1,
                 alpha_layers: int = 2,
                 alpha_reduction: int = 1,
                 delta_layers: int = 2,
                 delta_reduction: int = 1,
                 normalize_p: bool = False,
                 geom_mode: str = 'sub',
                 stride_mode: str = 'nearest',
                 dimension: int = 3,
                 norm_type: str = 'batch',
                 bn_momentum: float = 0.1,
                 activation: nn.Module = nn.LeakyReLU(0.1)):
        """
        v1 of point involution (naive and naiv+geom). It can includes geometric encodings
        Has the following problems:
            > attention weights are assigned to neighbors according to knn order

        N.B. Compared to the original paper 
            our "channels_per_group" = their     "groups" 
            our      "groups"        = their "group_channels"
        We changed for clarity and consistency with previous works like PointTransformers.

        Args:
            channels              (int): The number of the input=output channels.
            radius              (float): The radius used for geometric normalization.
            neighborhood_size     (int): The number of neighbors to be able to generate weights for each.
            groups              (int=8): number of groups in involution.
            channels_per_group (int=32): number of channels per group in involution. Ignored if group is specified
            alpha_layers        (int=2): number of layers in MLP alpha.
            alpha_reduction     (int=1): Reduction ratio for MLP alpha.
            double_delta (bool = False): Are we using double delta network (v4)
            normalize_p  (bool = False): Are we normalizing geometric data for encodings
            geom_mode       (str='add'): Mode for geometric encoding merge ('add', 'sub', 'mul', 'cat')
            stride_mode (str='nearest'): Mode for strided attention ('nearest', 'avg', 'max')
            dimension           (int=3): The dimension of the point space.
            norm_type     (str='batch'): type of normalization used in layer ('group', 'batch', 'none')
            bn_momentum    (float=0.10): Momentum for batch normalization
            activation (nn.Module|None=nn.LeakyReLU(0.1)): Activation function. Use None for no activation.
        """
        super(point_involution_v1, self).__init__()

        # Verification of group parameter
        if groups > 0:
            assert channels % groups == 0, "channels must be divisible by channels_per_group."
            channels_per_group = channels // groups
        else:
            assert channels % channels_per_group == 0, "channels must be divisible by channels_per_group."
            groups = channels // channels_per_group
        assert channels % alpha_reduction == 0, "channels must be divisible by reduction_ratio."

        # Save parameters
        self.channels = channels
        self.radius = radius
        self.neighborhood_size = neighborhood_size
        self.channels_per_group = channels_per_group
        self.groups = groups
        self.alpha_layers = alpha_layers
        self.alpha_reduction = alpha_reduction
        self.delta_layers = delta_layers
        self.delta_reduction = delta_reduction
        self.normalize_p = normalize_p
        self.geom_mode = geom_mode
        self.stride_mode = stride_mode
        self.dimension = dimension

        # Define MLP alpha
        C = channels
        R = alpha_reduction
        Cout = self.neighborhood_size * channels_per_group
        if alpha_layers < 2:
            self.alpha_mlp = nn.Sequential(NormBlock(C),
                                           activation,
                                           nn.Linear(C, Cout))

        else:
            self.alpha_mlp = nn.Sequential(NormBlock(C),
                                           activation,
                                           UnaryBlock(C, C // R, norm_type, bn_momentum, activation))
            for _ in range(alpha_layers - 2):
                self.alpha_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
            self.alpha_mlp.append(nn.Linear(C // R, Cout))

        # Define MLP delta
        D = self.dimension
        R = delta_reduction
        if delta_layers < 2:
            self.delta_mlp = nn.Linear(D, C)
        else:
            self.delta_mlp = nn.Sequential(UnaryBlock(D, C // R, norm_type, bn_momentum, activation))
            for _ in range(delta_layers - 2):
                self.delta_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
            self.delta_mlp.append(nn.Linear(C // R, C))
            
        # Define MLP gamma
        use_gamma_mlp = True
        if geom_mode == 'cat':
            self.gamma_mlp = nn.Sequential(NormBlock(2 * C),
                                           activation,
                                           nn.Linear(2 * C, C))
        elif use_gamma_mlp:
            self.gamma_mlp = nn.Sequential(NormBlock(C),
                                           activation,
                                           nn.Linear(C, C))
        else:
            self.gamma_mlp = nn.Identity()
        
        self.softmax = nn.Softmax(dim=1)
        self.sigmoid = nn.Sigmoid()

        self.use_geom = geom_mode in ['add', 'sub', 'mul', 'cat']

        return

    def forward(self, q_pts: Tensor,
                s_pts: Tensor,
                s_feats: Tensor,
                neighb_inds: Tensor) -> Tensor:
        """
        Args:
            q_points (Tensor): query points (M, 3).
            s_points (Tensor): support points carrying input features (N, 3).
            s_feats (Tensor): input features values (N, C_in).
            neighb_inds (LongTensor): neighbor indices of query points among support points (M, H).
        Returns:
            q_feats (Tensor): output features carried by query points (M, C_out).
        """

        # Shadow neighbors have to be handled via a mask
        # **********************************************

        with torch.no_grad():
            valid_mask = neighb_inds < int(s_feats.shape[0])
            shadow_bool = not torch.all(valid_mask).item()


        # Get features for each neighbor
        # ******************************

        # Get the features of each neighborhood
        padded_s_feats = torch.cat((s_feats, torch.zeros_like(s_feats[:1, :])), 0)  # (N, C) -> (N+1, C)
        neighb_v_feats = index_select(padded_s_feats, neighb_inds, dim=0)  # -> (M, H, C)


        # Get geometric encoding features
        # *******************************

        if self.use_geom:
        
            with torch.no_grad():

                # Add a fake point in the last row for shadow neighbors
                s_pts = torch.cat((s_pts, torch.zeros_like(s_pts[:1, :])), 0)   # (N, 3) -> (N+1, 3)

                # Get neighbor points [n_points, n_neighbors, dim]
                neighbors = index_select(s_pts, neighb_inds, dim=0)  # (N+1, 3) -> (M, H, 3)

                # Center every neighborhood
                neighbors = (neighbors - q_pts.unsqueeze(1))

                # Rescale for normalization
                if self.normalize_p:
                    neighbors *= 1 / self.radius   # -> (M, H, 3)
            
            # Generate geometric encodings
            geom_encodings = self.delta_mlp(neighbors) # (M, H, 3) -> (M, H, C)
                
            # Merge with features
            if self.geom_mode == 'add':
                neighb_v_feats += geom_encodings  # -> (M, H, C)
            elif self.geom_mode == 'sub':
                neighb_v_feats -= geom_encodings  # -> (M, H, C)
            elif self.geom_mode == 'mul':
                neighb_v_feats *= geom_encodings  # -> (M, H, C)
            elif self.geom_mode == 'cat':
                neighb_v_feats = torch.cat((neighb_v_feats, geom_encodings), dim=2)  # -> (M, H, 2C)

            # Final linear transform
            neighb_v_feats = self.gamma_mlp(neighb_v_feats) # (M, H, C) -> (M, H, C)


        # Get attention weights
        # *********************

        # Get query features from the center point
        if q_pts.shape[0] == s_pts.shape[0]:
            q_feats = s_feats  # In case M == N, supports and queries are the same
        elif self.stride_mode == 'nearest':
            q_feats = neighb_v_feats[:, 0, :]  # nearest pool (M, H, C) -> (M, C)
        elif self.stride_mode == 'max':
            q_feats = torch.max(neighb_v_feats, dim=1)  # max pool (M, H, C) -> (M, C)
        elif self.stride_mode == 'avg':
            q_feats = torch.mean(neighb_v_feats, dim=1)  # avg pool (M, H, C) -> (M, C)
        

        # Generate attention weights
        attention_weights = self.alpha_mlp(q_feats)  # (M, C) -> (M, H*CpG)
        H = int(neighb_inds.shape[1])
        attention_weights = attention_weights.view(-1, H, self.channels_per_group, 1)  # (M, H*CpG) -> (M, H, CpG, 1)
        attention_weights = self.softmax(attention_weights)

        

        # Apply attention weights
        # ************************

        # Separate features in groups
        neighb_v_feats = neighb_v_feats.view(-1, H, self.channels_per_group, self.groups)  # (M, H, C) -> (M, H, CpG, G)

        # Multiply features with attention
        neighb_v_feats *= attention_weights  # -> (M, H, CpG, G)

        # Apply shadow mask (every gradient for shadow neighbors will be zero)
        if shadow_bool:
            neighb_v_feats *= valid_mask.type(torch.float32).unsqueeze(2).unsqueeze(3)

        # Sum over neighbors
        output_feats = torch.sum(neighb_v_feats, dim=1)  # -> (M, CpG, G)
        
        # Reshape
        output_feats = output_feats.view(-1, self.channels)  # -> (M, C)

        return output_feats

    def __repr__(self):

        repr_str = 'point_involution_v1'
        repr_str += '(C: {:d}'.format(self.channels)
        repr_str += ', G: {:d})'.format(self.groups)

        return repr_str


class point_involution_v3(nn.Module):

    def __init__(self,
                 channels: int,
                 radius: float,
                 groups: int = 8,
                 channels_per_group: int = -1,
                 alpha_layers: int = 2,
                 alpha_reduction: int = 1,
                 delta_layers: int = 2,
                 delta_reduction: int = 1,
                 double_delta: bool = False,
                 normalize_p: bool = False,
                 geom_mode: str = 'sub',
                 stride_mode: str = 'nearest',
                 dimension: int = 3,
                 norm_type: str = 'batch',
                 bn_momentum: float = 0.1,
                 activation: nn.Module = nn.LeakyReLU(0.1)):
        """
        v3 of point involution. It includes geometric encodings in both features and attention branches

        N.B. Compared to the original paper 
            our "channels_per_group" = their     "groups" 
            our      "groups"        = their "group_channels"
        We changed for clarity and consistency with previous works like PointTransformers.

        Args:
            channels              (int): The number of the input=output channels.
            radius              (float): The radius used for geometric normalization.
            groups              (int=8): number of groups in involution.
            channels_per_group (int=32): number of channels per group in involution. Ignored if group is specified
            alpha_layers        (int=2): number of layers in MLP alpha.
            alpha_reduction     (int=1): Reduction ratio for MLP alpha.
            double_delta (bool = False): Are we using double delta network (v4)
            normalize_p  (bool = False): Are we normalizing geometric data for encodings
            geom_mode       (str='add'): Mode for geometric encoding merge ('add', 'sub', 'mul', 'cat')
            stride_mode (str='nearest'): Mode for strided attention ('nearest', 'avg', 'max')
            dimension           (int=3): The dimension of the point space.
            norm_type     (str='batch'): type of normalization used in layer ('group', 'batch', 'none')
            bn_momentum    (float=0.10): Momentum for batch normalization
            activation (nn.Module|None=nn.LeakyReLU(0.1)): Activation function. Use None for no activation.
        """
        super(point_involution_v3, self).__init__()

        # Verification of group parameter
        if groups > 0:
            assert channels % groups == 0, "channels must be divisible by channels_per_group."
            channels_per_group = channels // groups
        else:
            assert channels % channels_per_group == 0, "channels must be divisible by channels_per_group."
            groups = channels // channels_per_group
        assert channels % alpha_reduction == 0, "channels must be divisible by reduction_ratio."

        # Save parameters
        self.channels = channels
        self.radius = radius
        self.channels_per_group = channels_per_group
        self.groups = groups
        self.alpha_layers = alpha_layers
        self.alpha_reduction = alpha_reduction
        self.delta_layers = delta_layers
        self.delta_reduction = delta_reduction
        self.double_delta = double_delta
        self.normalize_p = normalize_p
        self.geom_mode = geom_mode
        self.stride_mode = stride_mode
        self.dimension = dimension

        # Define MLP alpha
        C = channels
        R = alpha_reduction
        CpG = channels_per_group
        if geom_mode == 'cat':
            Cin = 2 * C
        else:
            Cin = C

        if alpha_layers < 2:
            self.alpha_mlp = nn.Sequential(NormBlock(Cin),
                                           activation,
                                           nn.Linear(Cin, CpG))

        else:
            self.alpha_mlp = nn.Sequential(NormBlock(Cin),
                                           activation,
                                           UnaryBlock(Cin, C // R, norm_type, bn_momentum, activation))
            for _ in range(alpha_layers - 2):
                self.alpha_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
            self.alpha_mlp.append(nn.Linear(C // R, CpG))

        # Define MLP delta
        D = self.dimension
        R = delta_reduction
        if delta_layers < 2:
            self.delta_mlp = nn.Linear(D, C)
        else:
            self.delta_mlp = nn.Sequential(UnaryBlock(D, C // R, norm_type, bn_momentum, activation))
            for _ in range(delta_layers - 2):
                self.delta_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
            self.delta_mlp.append(nn.Linear(C // R, C))
        if double_delta:
            if delta_layers < 2:
                self.delta2_mlp = nn.Linear(D, C)
            else:
                self.delta2_mlp = nn.Sequential(UnaryBlock(D, C // R, norm_type, bn_momentum, activation))
                for _ in range(delta_layers - 2):
                    self.delta2_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
                self.delta2_mlp.append(nn.Linear(C // R, C))


        # Define MLP gamma
        use_gamma_mlp = True
        if geom_mode == 'cat':
            self.gamma_mlp = nn.Sequential(NormBlock(2 * C),
                                           activation,
                                           nn.Linear(2 * C, C))
        elif use_gamma_mlp:
            self.gamma_mlp = nn.Sequential(NormBlock(C),
                                           activation,
                                           nn.Linear(C, C))
        else:
            self.gamma_mlp = nn.Identity()
        
        self.softmax = nn.Softmax(dim=1)
        self.sigmoid = nn.Sigmoid()
        self.activation =activation

        return

    def forward(self, q_pts: Tensor,
                s_pts: Tensor,
                s_feats: Tensor,
                neighb_inds: Tensor) -> Tensor:
        """
        Args:
            q_points (Tensor): query points (M, 3).
            s_points (Tensor): support points carrying input features (N, 3).
            s_feats (Tensor): input features values (N, C_in).
            neighb_inds (LongTensor): neighbor indices of query points among support points (M, H).
        Returns:
            q_feats (Tensor): output features carried by query points (M, C_out).
        """

        # Shadow neighbors have to be handled via a mask
        # **********************************************

        with torch.no_grad():
            valid_mask = neighb_inds < int(s_feats.shape[0])
            shadow_bool = not torch.all(valid_mask).item()


        # Get features for each neighbor
        # ******************************

        # Get the features of each neighborhood
        padded_s_feats = torch.cat((s_feats, torch.zeros_like(s_feats[:1, :])), 0)  # (N, C) -> (N+1, C)
        neighb_v_feats = index_select(padded_s_feats, neighb_inds, dim=0)  # -> (M, H, C)


        # Get geometric encoding features
        # *******************************
        
        with torch.no_grad():

            # Add a fake point in the last row for shadow neighbors
            s_pts = torch.cat((s_pts, torch.zeros_like(s_pts[:1, :])), 0)   # (N, 3) -> (N+1, 3)

            # Get neighbor points [n_points, n_neighbors, dim]
            neighbors = index_select(s_pts, neighb_inds, dim=0)  # (N+1, 3) -> (M, H, 3)

            # Center every neighborhood
            neighbors = (neighbors - q_pts.unsqueeze(1))

            # Rescale for normalization
            if self.normalize_p:
                neighbors *= 1 / self.radius   # -> (M, H, 3)
        
        # Generate geometric encodings
        geom_encodings = self.delta_mlp(neighbors) # (M, H, 3) -> (M, H, C)
        if self.double_delta:
            geom_encodings2 = self.delta2_mlp(neighbors) # (M, H, 3) -> (M, H, C)
        else:
            geom_encodings2 = geom_encodings
            
        # Merge with features
        if self.geom_mode == 'add':
            neighb_v_feats += geom_encodings  # -> (M, H, C)
        elif self.geom_mode == 'sub':
            neighb_v_feats -= geom_encodings  # -> (M, H, C)
        elif self.geom_mode == 'mul':
            neighb_v_feats *= geom_encodings  # -> (M, H, C)
        elif self.geom_mode == 'cat':
            neighb_v_feats = torch.cat((neighb_v_feats, geom_encodings), dim=2)  # -> (M, H, 2C)

        # Final linear transform
        neighb_v_feats = self.gamma_mlp(neighb_v_feats) # (M, H, C) -> (M, H, C)


        # Get attention weights
        # *********************

        # Get query features from the center point
        if q_pts.shape[0] == s_pts.shape[0]:
            q_feats = s_feats  # In case M == N, supports and queries are the same
        elif self.stride_mode == 'nearest':
            q_feats = neighb_v_feats[:, 0, :]  # nearest pool (M, H, C) -> (M, C)
        elif self.stride_mode == 'max':
            q_feats = torch.max(neighb_v_feats, dim=1)  # max pool (M, H, C) -> (M, C)
        elif self.stride_mode == 'avg':
            q_feats = torch.mean(neighb_v_feats, dim=1)  # avg pool (M, H, C) -> (M, C)
        
        # Merge geometric encodings with feature
        q_feats = q_feats.unsqueeze(1)    # -> (M, 1, C)
        if self.geom_mode == 'add':
            q_feats = q_feats + geom_encodings2  # -> (M, H, C)
        elif self.geom_mode == 'sub':
            q_feats = q_feats - geom_encodings2  # -> (M, H, C)
        elif self.geom_mode == 'mul':
            q_feats = q_feats * geom_encodings2  # -> (M, H, C)
        elif self.geom_mode == 'cat':
            q_feats = torch.cat((q_feats, geom_encodings2), dim=2)  # -> (M, H, 2C)

        # Generate attention weights
        attention_weights = self.alpha_mlp(q_feats)  # (M, H, C) -> (M, H, CpG)
        attention_weights = self.softmax(attention_weights)

        
        # Apply attention weights
        # ************************

        # Separate features in groups
        H = int(neighb_inds.shape[1])
        neighb_v_feats = neighb_v_feats.view(-1, H, self.channels_per_group, self.groups)  # (M, H, C) -> (M, H, CpG, G)
        attention_weights = attention_weights.view(-1, H, self.channels_per_group, 1)  # (M, H, CpG) -> (M, H, CpG, 1)

        # Multiply features with attention
        neighb_v_feats *= attention_weights  # -> (M, H, CpG, G)

        # Apply shadow mask (every gradient for shadow neighbors will be zero)
        if shadow_bool:
            neighb_v_feats *= valid_mask.type(torch.float32).unsqueeze(2).unsqueeze(3)

        # Sum over neighbors
        output_feats = torch.sum(neighb_v_feats, dim=1)  # -> (M, CpG, G)
        
        # Reshape
        output_feats = output_feats.view(-1, self.channels)  # -> (M, C)

        return output_feats

    def __repr__(self):

        repr_str = 'point_involution_v3'
        repr_str += '(C: {:d}'.format(self.channels)
        repr_str += ', G: {:d})'.format(self.groups)

        return repr_str


class point_transformer(nn.Module):

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 radius: float,
                 groups: int = 8,
                 channels_per_group: int = -1,
                 alpha_layers: int = 2,
                 delta_layers: int = 2,
                 delta_reduction: int = 4,
                 double_delta: bool = False,
                 normalize_p: bool = False,
                 geom_mode: str = 'sub',
                 stride_mode: str = 'nearest',
                 dimension: int = 3,
                 norm_type: str = 'batch',
                 bn_momentum: float = 0.1,
                 activation: nn.Module = nn.LeakyReLU(0.1)):
        """
        Reimplementation of point_transformer. Basically a point_involution_v3, with keys that introduce features in the attention process.

        List of difference:
            - delta MLP: we have 3 -> C/4 -> C  instead of  3 -> 3 -> C
            - possibility of double delta
            - possibility of normalize p_c

        Args:
            in_channels           (int): The number of input channels.
            out_channels          (int): The number of output channels.
            radius              (float): The radius used for geometric normalization.
            groups              (int=8): number of groups in involution.
            channels_per_group (int=32): number of channels per group in involution. Ignored if group is specified
            alpha_layers        (int=2): number of layers in MLP alpha.
            double_delta (bool = False): Are we using double delta network (v4)
            normalize_p  (bool = False): Are we normalizing geometric data for encodings
            geom_mode       (str='add'): Mode for geometric encoding merge ('add', 'sub', 'mul', 'cat')
            stride_mode (str='nearest'): Mode for strided attention ('nearest', 'avg', 'max')
            dimension           (int=3): The dimension of the point space.
            norm_type     (str='batch'): type of normalization used in layer ('group', 'batch', 'none')
            bn_momentum    (float=0.10): Momentum for batch normalization
        """
        super(point_transformer, self).__init__()

        # Verification of group parameter
        if groups > 0:
            assert out_channels % groups == 0, "channels must be divisible by channels_per_group."
            channels_per_group = out_channels // groups
        else:
            assert out_channels % channels_per_group == 0, "channels must be divisible by channels_per_group."
            groups = out_channels // channels_per_group

        # Save parameters
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.radius = radius
        self.channels_per_group = channels_per_group
        self.groups = groups
        self.alpha_layers = alpha_layers
        self.delta_layers = delta_layers
        self.delta_reduction = delta_reduction
        self.double_delta = double_delta
        self.normalize_p = normalize_p
        self.geom_mode = geom_mode
        self.stride_mode = stride_mode
        self.dimension = dimension
        self.activation = activation

        # Define first linear transforms
        Cin = in_channels
        C = out_channels
        self.linear_q = nn.Linear(Cin, C)
        self.linear_k = nn.Linear(Cin, C)
        self.linear_v = nn.Linear(Cin, C)

        # Define MLP alpha
        CpG = channels_per_group
        if geom_mode == 'cat':
            Cin = 2 * C
        else:
            Cin = C
        if alpha_layers < 2:
            self.alpha_mlp = nn.Sequential(NormBlock(Cin),
                                           activation,
                                           nn.Linear(Cin, CpG))
        else:
            self.alpha_mlp = nn.Sequential(NormBlock(Cin),
                                           activation,
                                           UnaryBlock(Cin, CpG, norm_type, bn_momentum, activation))
            for _ in range(alpha_layers - 2):
                self.alpha_mlp.append(UnaryBlock(CpG, CpG, norm_type, bn_momentum, activation))
            self.alpha_mlp.append(nn.Linear(CpG, CpG))

        # Define MLP delta
        D = self.dimension
        R = delta_reduction
        if delta_layers < 2:
            self.delta_mlp = nn.Linear(D, C)
        else:
            self.delta_mlp = nn.Sequential(UnaryBlock(D, C // R, norm_type, bn_momentum, activation))
            for _ in range(delta_layers - 2):
                self.delta_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
            self.delta_mlp.append(nn.Linear(C // R, C))
        if double_delta:
            if delta_layers < 2:
                self.delta2_mlp = nn.Linear(D, C)
            else:
                self.delta2_mlp = nn.Sequential(UnaryBlock(D, C // R, norm_type, bn_momentum, activation))
                for _ in range(delta_layers - 2):
                    self.delta2_mlp.append(UnaryBlock(C // R, C // R, norm_type, bn_momentum, activation))
                self.delta2_mlp.append(nn.Linear(C // R, C))

        # Define MLP gamma
        use_gamma_mlp = False
        if geom_mode == 'cat':
            self.gamma_mlp = nn.Linear(2 * C, C)
        elif use_gamma_mlp:
            self.gamma_mlp = nn.Linear(C, C)
        else:
            self.gamma_mlp = nn.Identity()
        
        self.softmax = nn.Softmax(dim=1)
        
        # Set this to false to have something very similar to involution (some additional linears)
        self.use_k_feats = True

        return

    def forward(self, q_pts: Tensor,
                s_pts: Tensor,
                s_feats: Tensor,
                neighb_inds: Tensor) -> Tensor:
        """
        Args:
            q_points (Tensor): query points (M, 3).
            s_points (Tensor): support points carrying input features (N, 3).
            s_feats (Tensor): input features values (N, C_in).
            neighb_inds (LongTensor): neighbor indices of query points among support points (M, H).
        Returns:
            q_feats (Tensor): output features carried by query points (M, C_out).
        """

        # Shadow neighbors have to be handled via a mask
        # **********************************************

        with torch.no_grad():
            valid_mask = neighb_inds < int(s_feats.shape[0])
            shadow_bool = not torch.all(valid_mask).item()


        # Get features for each neighbor
        # ******************************

        # Get the features of each neighborhood
        v_feats = self.linear_v(s_feats)
        padded_v_feats = torch.cat((v_feats, torch.zeros_like(v_feats[:1, :])), 0)  # (N, C) -> (N+1, C)
        neighb_v_feats = index_select(padded_v_feats, neighb_inds, dim=0)  # -> (M, H, C)

        # Get keys features from neighbors
        if self.use_k_feats:
            k_feats = self.linear_k(s_feats)
            padded_k_feats = torch.cat((k_feats, torch.zeros_like(k_feats[:1, :])), 0)  # (N, C) -> (N+1, C)
            neighb_k_feats = index_select(padded_k_feats, neighb_inds, dim=0)  # -> (M, H, C)

        # Get geometric encoding features
        # *******************************
        
        with torch.no_grad():

            # Add a fake point in the last row for shadow neighbors
            s_pts = torch.cat((s_pts, torch.zeros_like(s_pts[:1, :])), 0)   # (N, 3) -> (N+1, 3)

            # Get neighbor points [n_points, n_neighbors, dim]
            neighbors = index_select(s_pts, neighb_inds, dim=0)  # (N+1, 3) -> (M, H, 3)

            # Center every neighborhood
            neighbors = (neighbors - q_pts.unsqueeze(1))

            # Rescale for normalization
            if self.normalize_p:
                neighbors *= 1 / self.radius   # -> (M, H, 3)
        
        # Generate geometric encodings
        geom_encodings = self.delta_mlp(neighbors) # (M, H, 3) -> (M, H, C)
        if self.double_delta:
            geom_encodings2 = self.delta2_mlp(neighbors) # (M, H, 3) -> (M, H, C)
        else:
            geom_encodings2 = geom_encodings
        
        # Merge with features
        if self.geom_mode == 'add':
            neighb_v_feats += geom_encodings  # -> (M, H, C)
        elif self.geom_mode == 'sub':
            neighb_v_feats -= geom_encodings  # -> (M, H, C)
        elif self.geom_mode == 'mul':
            neighb_v_feats *= geom_encodings  # -> (M, H, C)
        elif self.geom_mode == 'cat':
            neighb_v_feats = torch.cat((neighb_v_feats, geom_encodings), dim=2)  # -> (M, H, 2C)

        # Final linear transform
        neighb_v_feats = self.gamma_mlp(neighb_v_feats) # (M, H, C) -> (M, H, C)


        # Get attention weights
        # *********************

        # Get query features from the center point
        q_feats = self.linear_q(s_feats)

        # In case M != N, pool features to query positions
        if q_pts.shape[0] != s_pts.shape[0]:
            padded_q_feats = torch.cat((q_feats, torch.zeros_like(q_feats[:1, :])), 0)  # (N, C) -> (N+1, C)
            if self.stride_mode == 'nearest':
                q_feats = index_select(padded_q_feats, neighb_inds[:, 0], dim=0)  # nearest pool -> (M, C)
            elif self.stride_mode == 'max':
                q_feats = torch.max(index_select(padded_q_feats, neighb_inds, dim=0), dim=1)  # max pool (M, H, C) -> (M, C)
            elif self.stride_mode == 'avg':
                q_feats = torch.mean(index_select(padded_q_feats, neighb_inds, dim=0), dim=1)  # avg pool (M, H, C) -> (M, C)

        # Merge with keys
        qk_feats = q_feats.unsqueeze(1)    # -> (M, 1, C)
        if self.use_k_feats:
            qk_feats = qk_feats - neighb_k_feats  # -> (M, H, C)

        # Merge geometric encodings with feature
        if self.geom_mode == 'add':
            qk_feats = qk_feats + geom_encodings2  # -> (M, H, C)
        elif self.geom_mode == 'sub':
            qk_feats = qk_feats - geom_encodings2  # -> (M, H, C)
        elif self.geom_mode == 'mul':
            qk_feats = qk_feats * geom_encodings2  # -> (M, H, C)
        elif self.geom_mode == 'cat':
            qk_feats = torch.cat((qk_feats, geom_encodings2), dim=2)  # -> (M, H, 2C)

        # Generate attention weights
        attention_weights = self.alpha_mlp(qk_feats) # (M, H, C) -> (M, H, CpG)
        attention_weights = self.softmax(attention_weights)

        # Apply attention weights
        # ************************

        # Separate features in groups
        H = int(neighb_inds.shape[1])
        neighb_v_feats = neighb_v_feats.view(-1, H, self.channels_per_group, self.groups)  # (M, H, C) -> (M, H, CpG, G)
        attention_weights = attention_weights.view(-1, H, self.channels_per_group, 1)  # (M, H*CpG) -> (M, H, CpG, 1)

        # Multiply features with attention
        neighb_v_feats *= attention_weights  # -> (M, H, CpG, G)

        # Apply shadow mask (every gradient for shadow neighbors will be zero)
        if shadow_bool:
            # print('shadow neighbors present', s_feats.shape)
            neighb_v_feats *= valid_mask.type(torch.float32).unsqueeze(2).unsqueeze(3)

        # Sum over neighbors
        output_feats = torch.sum(neighb_v_feats, dim=1)  # -> (M, CpG, G)
        
        # Reshape
        output_feats = output_feats.view(-1, self.out_channels)  # -> (M, C)

        return output_feats

    def __repr__(self):

        repr_str = 'point_transformer'
        repr_str += '(Cin: {:d}'.format(self.in_channels)
        repr_str += ', Cout: {:d}'.format(self.out_channels)
        repr_str += ', G: {:d})'.format(self.groups)

        return repr_str




# ----------------------------------------------------------------------------------------------------------------------
#
#           Complex blocks
#       \********************/
#

class InvolutionBlock(nn.Module):

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 radius: float,
                 neighborhood_size: int,
                 inv_mode: str,
                 groups: int = 8,
                 dimension: int = 3,
                 norm_type: str = 'batch',
                 bn_momentum: float = 0.1,
                 activation: nn.Module = nn.LeakyReLU(0.1)):
        """
        InvolutionBlock block with normalization and activation.  
        Args:
            in_channels           (int): The number of input channels.
            out_channels          (int): The number of output channels.
            radius              (float): convolution radius
            neighborhood_size     (int): number of neighbor points
            inv_mode              (str): type of involution used in layer
            groups              (int=8): number of groups in involution.
            dimension           (int=3): dimension of input
            norm_type     (str='batch'): type of normalization used in layer ('group', 'batch', 'none')
            bn_momentum    (float=0.10): Momentum for batch normalization
            activation (nn.Module|None=nn.LeakyReLU(0.1)): Activation function. Use None for no activation.
        """
        super(InvolutionBlock, self).__init__()

        # Define parameters
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.radius = radius
        self.neighborhood_size = neighborhood_size
        self.inv_mode = inv_mode

        # Define modules
        self.activation = activation
        self.norm = NormBlock(out_channels, norm_type, bn_momentum)

        if inv_mode == 'inv_v1':
            assert in_channels == out_channels
            self.involution = point_involution_v1(out_channels,
                                                  neighborhood_size,
                                                  radius,
                                                  groups=groups,
                                                  geom_mode='none',
                                                  dimension=dimension,
                                                  norm_type=norm_type,
                                                  bn_momentum=bn_momentum)

        elif inv_mode == 'inv_v2':
            assert in_channels == out_channels
            self.involution = point_involution_v1(out_channels,
                                                  neighborhood_size,
                                                  radius,
                                                  groups=groups,
                                                  dimension=dimension,
                                                  norm_type=norm_type,
                                                  bn_momentum=bn_momentum)

        elif inv_mode == 'inv_v3':
            assert in_channels == out_channels
            self.involution = point_involution_v3(out_channels,
                                                  radius,
                                                  groups=groups,
                                                  double_delta=False,
                                                  dimension=dimension,
                                                  norm_type=norm_type,
                                                  bn_momentum=bn_momentum)

        elif inv_mode == 'inv_v4':
            self.involution = point_involution_v3(out_channels,
                                                  radius,
                                                  groups=groups,
                                                  double_delta=True,
                                                  dimension=dimension,
                                                  norm_type=norm_type,
                                                  bn_momentum=bn_momentum)

        elif inv_mode == 'transformer':
            self.involution = point_transformer(in_channels,
                                                out_channels,
                                                radius,
                                                groups=groups,
                                                dimension=dimension,
                                                norm_type=norm_type,
                                                bn_momentum=bn_momentum)


        return
        
    def forward(self, q_pts, s_pts, s_feats, neighbor_indices):
        q_feats = self.involution(q_pts, s_pts, s_feats, neighbor_indices)
        q_feats = self.norm(q_feats)
        q_feats = self.activation(q_feats)
        return q_feats

     
    def __repr__(self):
        return 'KPInvBlock(C: {:d} -> {:d}, r: {:.2f})'.format(self.in_channels,
                                                                         self.out_channels,
                                                                         self.radius)


class InvolutionResidualBlock(nn.Module):

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 radius: float,
                 neighborhood_size: int,
                 inv_mode: str,
                 groups: int = 16,
                 strided: bool = False,
                 dimension: int = 3,
                 norm_type: str = 'batch',
                 bn_momentum: float = 0.1,
                 activation: nn.Module = nn.LeakyReLU(0.1)):
        """
        Involution residual bottleneck block.
        Args:
            in_channels           (int): The number of input channels.
            out_channels          (int): The number of output channels.
            radius              (float): convolution radius
            neighborhood_size     (int): number of neighbor points
            inv_mode              (str): type of involution used in layer
            groups              (int=8): number of groups in involution.
            strided (bool=False): strided or not
            dimension           (int=3): dimension of input
            norm_type     (str='batch'): type of normalization used in layer ('group', 'batch', 'none')
            bn_momentum    (float=0.10): Momentum for batch normalization
            activation (nn.Module|None=nn.LeakyReLU(0.1)): Activation function. Use None for no activation.
        """
        super(InvolutionResidualBlock, self).__init__()

        # Define parameters
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.strided = strided
        self.bn_momentum = bn_momentum
        self.norm_type = norm_type

        mid_channels = out_channels // 4
        
        # First downscaling mlp
        if in_channels != mid_channels:
            self.unary1 = UnaryBlock(in_channels, mid_channels, norm_type, bn_momentum)
        else:
            self.unary1 = nn.Identity()

        # KPInv block with normalization and activation
        self.conv = InvolutionBlock(mid_channels,
                                    mid_channels,
                                    radius,
                                    neighborhood_size,
                                    inv_mode=inv_mode,
                                    groups=groups,
                                    dimension=dimension,
                                    norm_type=norm_type,
                                    bn_momentum=bn_momentum,
                                    activation=activation)

        # Second upscaling mlp
        self.unary2 = UnaryBlock(mid_channels, out_channels, norm_type, bn_momentum, activation=None)

        # Shortcut optional mpl
        if in_channels != out_channels:
            self.unary_shortcut = UnaryBlock(in_channels, out_channels, norm_type, bn_momentum, activation=None)
        else:
            self.unary_shortcut = nn.Identity()

        # Final activation function
        self.activation = activation

        return

    def forward(self, q_pts, s_pts, s_feats, neighbor_indices):
        
        # First downscaling mlp
        x = self.unary1(s_feats)

        # Convolution
        x = self.conv(q_pts, s_pts, x, neighbor_indices)

        # Second upscaling mlp
        x = self.unary2(x)

        # Shortcut
        if self.strided:
            shortcut = local_maxpool(s_feats, neighbor_indices)
        else:
            shortcut = s_feats
        shortcut = self.unary_shortcut(shortcut)

        # Final activation
        q_feats = x + shortcut
        q_feats = self.activation(q_feats)

        return q_feats




