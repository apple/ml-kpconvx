#
# For licensing see accompanying LICENSE file.
# Copyright (C) 2024 Apple Inc. All Rights Reserved.
#
# ----------------------------------------------------------------------------------------------------------------------
#
#   Hugues THOMAS - 06/10/2023
#
#   KPConvX project: KPNext.py
#       > Define the network architecture for KPConvX
#

import time
import torch
import torch.nn as nn
import numpy as np

from models.generic_blocks import LinearUpsampleBlock, NearestUpsampleBlock, UnaryBlock, local_nearest_pool, GlobalAverageBlock, MaxPoolBlock, SmoothCrossEntropyLoss
from models.kpconv_blocks import KPConvBlock, KPConvResidualBlock, KPConvInvertedBlock
from models.kpnext_blocks import KPNextResidualBlock, KPNextInvertedBlock, KPNextMultiShortcutBlock, KPNextBlock

from utils.torch_pyramid import fill_pyramid

class KPNeXt(nn.Module):

    def __init__(self, cfg):
        """
        Class defining KPNeXt, a modern architecture inspired from ConvNext.
        Standard drop_path_rate: 0
        Standard layer_scale_init_value: 1e-6
        Standard head_init_scale: 1

        Args:
            cfg (EasyDict): configuration dictionary
        """
        super(KPNeXt, self).__init__()

        ############
        # Parameters
        ############

        # Parameters
        self.subsample_size = cfg.model.in_sub_size
        if self.subsample_size < 0:
            self.subsample_size = cfg.data.init_sub_size
        self.in_sub_mode = cfg.model.in_sub_mode
        self.kp_radius = cfg.model.kp_radius
        self.kp_sigma = cfg.model.kp_sigma
        self.neighbor_limits = cfg.model.neighbor_limits
        if cfg.model.in_sub_size > cfg.data.init_sub_size * 1.01:
            self.first_radius = cfg.model.in_sub_size * cfg.model.kp_radius
        else:
            self.first_radius = cfg.data.init_sub_size * cfg.model.kp_radius
        self.radius_scaling = cfg.model.radius_scaling
        self.first_sigma = cfg.data.init_sub_size * self.kp_sigma

        self.layer_blocks = cfg.model.layer_blocks
        self.num_layers = len(self.layer_blocks)
        self.upsample_n = cfg.model.upsample_n
        self.share_kp = cfg.model.share_kp
        self.kp_mode = cfg.model.kp_mode
        self.task = cfg.data.task
        self.grid_pool = cfg.model.grid_pool
        self.add_decoder_layer = cfg.model.decoder_layer

        # Stochastic depth decay rule
        dpr_list = np.linspace(0, cfg.model.drop_path_rate, sum(self.layer_blocks)) 
        
        # List of valid labels (those not ignored in loss)
        self.valid_labels = np.sort([c for c in cfg.data.label_values if c not in cfg.data.ignored_labels])
        self.num_logits = len(self.valid_labels)

        # Variables
        in_C = cfg.model.input_channels
        first_C = cfg.model.init_channels
        conv_r = self.first_radius
        conv_sig = self.first_sigma
        channel_scaling = 2
        if 'channel_scaling' in cfg.model:
            channel_scaling = cfg.model.channel_scaling

        # Get channels at each layer
        layer_C = []
        for l in range(self.num_layers):
            target_C = first_C * channel_scaling ** l                   # Scale channels
            layer_C.append(int(np.ceil((target_C - 0.1) / 16)) * 16)    # Ensure it is divisible by 16 (even the first one)

        # Verify the architecture validity
        if self.layer_blocks[0] < 1:
            raise ValueError('First layer must contain at least 1 convolutional layers')
        if np.min(self.layer_blocks) < 1:
            raise ValueError('Each layer must contain at least 1 convolutional layers')
        
        #####################
        # List Encoder blocks
        #####################

        # ------ Layers 1 ------
        if cfg.model.share_kp:
            self.shared_kp = [{} for _ in range(self.num_layers)]
        else:
            self.shared_kp = [None for _ in range(self.num_layers)]

        # Initial convolution or MLP
        C = layer_C[0]
        self.stem = self.get_conv_block(in_C, C, conv_r, conv_sig, cfg)
        # self.stem = self.get_unary_block(in_C, C, cfg)

        # Next blocks
        self.encoder_1 = nn.ModuleList()
        use_conv = cfg.model.first_inv_layer >= 1
        for block_i in range(self.layer_blocks[0]):
            Cout = layer_C[1] if self.grid_pool and block_i == self.layer_blocks[0] - 1 else C
            self.encoder_1.append(self.get_residual_block(C, Cout, conv_r, conv_sig, cfg,
                                                          shared_kp_data=self.shared_kp[0],
                                                          conv_layer=use_conv,
                                                          drop_path=dpr_list[block_i]))

        # Pooling block
        self.pooling_1 = self.get_pooling_block(C, layer_C[1], conv_r, conv_sig, cfg,
                                                use_mod=(not use_conv))

        # ------ Layers [2, 3, 4, 5] ------
        for layer in range(2, self.num_layers + 1):
            l = layer - 1

            # Update features, radius, sigma for this layer
            C = layer_C[l]
            conv_r *= self.radius_scaling
            conv_sig *= self.radius_scaling

            # Layer blocks
            use_conv = cfg.model.first_inv_layer >= layer
            encoder_i = nn.ModuleList()
            for block_i in range(self.layer_blocks[l]):
                Cout = layer_C[l+1] if self.grid_pool and layer < self.num_layers and block_i == self.layer_blocks[l] - 1 else C
                encoder_i.append(self.get_residual_block(C, Cout, conv_r, conv_sig, cfg,
                                                         shared_kp_data=self.shared_kp[l],
                                                         conv_layer=use_conv,
                                                         drop_path=dpr_list[sum(self.layer_blocks[:l]) + block_i]))
            setattr(self, 'encoder_{:d}'.format(layer), encoder_i)

            # Pooling block (not for the last layer)
            if layer < self.num_layers:
                pooling_i = self.get_pooling_block(C, layer_C[l+1], conv_r, conv_sig, cfg,
                                                   use_mod=(not use_conv))

                setattr(self, 'pooling_{:d}'.format(layer), pooling_i)

        #####################
        # List Decoder blocks
        #####################

        if cfg.data.task == 'classification':

            #  ------ Head ------

            # Global pooling
            self.global_pooling = GlobalAverageBlock()

            # New head
            self.head = nn.Sequential(self.get_unary_block(layer_C[-1], 256, cfg, norm_type='none'),
                                      nn.Dropout(0.4),
                                      nn.Linear(256, self.num_logits))

            # # Old head
            # self.head = nn.Sequential(self.get_unary_block(layer_C[-1], layer_C[-1], cfg),
            #                           nn.Linear(layer_C[-1], self.num_logits))

        elif cfg.data.task == 'cloud_segmentation':

            # ------ Layers [4, 3, 2, 1] ------
            for layer in range(self.num_layers - 1, 0, -1):

                # Upsample block
                if self.grid_pool:
                    upsampling_i = NearestUpsampleBlock()
                else:
                    upsampling_i = LinearUpsampleBlock(self.upsample_n)
                setattr(self, 'upsampling_{:d}'.format(layer), upsampling_i)

                # Network layers in decoder
                C = layer_C[layer - 1]
                C1 = layer_C[layer]
                if self.grid_pool:
                    Cin = C1 + C1
                else:
                    Cin = C + C1
                decoder_unary_i = self.get_unary_block(Cin, C, cfg)
                setattr(self, 'decoder_unary_{:d}'.format(layer), decoder_unary_i)

                # Additionnal network layer (optional)
                if self.add_decoder_layer:
                    conv_r *= 1 / self.radius_scaling
                    conv_sig *= 1 / self.radius_scaling
                    decoder_layer_i = self.get_residual_block(C, C, conv_r, conv_sig, cfg,
                                                              shared_kp_data=self.shared_kp[layer - 1])
                    setattr(self, 'decoder_layer_{:d}'.format(layer), decoder_layer_i)


            #  ------ Head ------
            
            # New head
            self.head = nn.Sequential(self.get_unary_block(layer_C[0], layer_C[0], cfg),
                                    nn.Linear(layer_C[0], self.num_logits))
            # Easy KPConv Head
            # self.head = nn.Sequential(nn.Linear(layer_C[0] * 2, layer_C[0]),
            #                           nn.GroupNorm(8, layer_C[0]),
            #                           nn.ReLU(),
            #                           nn.Linear(layer_C[0], self.num_logits))

            # My old head
            # self.head = nn.Sequential(self.get_unary_block(layer_C[0] * 2, layer_C[0], cfg, norm_type='none'),
            #                           nn.Linear(layer_C[0], self.num_logits))



        ################
        # Network Losses
        ################

        # Choose between normal cross entropy and smoothed labels
        if cfg.train.smooth_labels:
            CrossEntropy = SmoothCrossEntropyLoss
        else:
            CrossEntropy = torch.nn.CrossEntropyLoss

        if cfg.data.task == 'classification':
            self.criterion = CrossEntropy()
            
        elif cfg.data.task == 'cloud_segmentation':
            if len(cfg.train.class_w) > 0:
                class_w = torch.from_numpy(np.array(cfg.train.class_w, dtype=np.float32))
                self.criterion = CrossEntropy(weight=class_w, ignore_index=-1)
            else:
                self.criterion = CrossEntropy(ignore_index=-1)

        # self.deform_fitting_mode = config.deform_fitting_mode
        # self.deform_fitting_power = config.deform_fitting_power
        # self.deform_lr_factor = config.deform_lr_factor
        # self.repulse_extent = config.repulse_extent

        self.deform_loss_factor = cfg.train.deform_loss_factor
        self.fit_rep_ratio = cfg.train.deform_fit_rep_ratio
        self.output_loss = 0
        self.deform_loss = 0
        self.l1 = nn.L1Loss()

        return

    def get_unary_block(self, in_C, out_C, cfg, norm_type=None):

        if norm_type is None:
            norm_type = cfg.model.norm

        return UnaryBlock(in_C,
                          out_C,
                          norm_type=norm_type,
                          bn_momentum=cfg.model.bn_momentum)

    def get_conv_block(self, in_C, out_C, radius, sigma, cfg):

        # First layer is the most simple convolution possible
        return KPConvBlock(in_C,
                           out_C,
                           cfg.model.shell_sizes,
                           radius,
                           sigma,
                           influence_mode=cfg.model.kp_influence,
                           aggregation_mode=cfg.model.kp_aggregation,
                           dimension=cfg.data.dim,
                           norm_type=cfg.model.norm,
                           bn_momentum=cfg.model.bn_momentum)

    def get_pooling_block(self, in_C, out_C, radius, sigma, cfg, use_mod=False):

        if self.grid_pool:
            return MaxPoolBlock()

        else:
            # Depthwise conv 
            if cfg.model.use_strided_conv:
                return KPConvBlock(in_C,
                                out_C,
                                cfg.model.shell_sizes,
                                radius,
                                sigma,
                                influence_mode=cfg.model.kp_influence,
                                aggregation_mode=cfg.model.kp_aggregation,
                                dimension=cfg.data.dim,
                                norm_type=cfg.model.norm,
                                bn_momentum=cfg.model.bn_momentum)

            else:
                attention_groups = cfg.model.inv_groups
                if 'kpconvd' in self.kp_mode or not use_mod:
                    attention_groups = 0
                return KPNextBlock(in_C,
                                out_C,
                                cfg.model.shell_sizes,
                                radius,
                                sigma,
                                attention_groups=attention_groups,
                                attention_act=cfg.model.inv_act,
                                mod_grp_norm=cfg.model.inv_grp_norm,
                                influence_mode=cfg.model.kp_influence,
                                dimension=cfg.data.dim,
                                norm_type=cfg.model.norm,
                                bn_momentum=cfg.model.bn_momentum)
                           
    def get_residual_block(self, in_C, out_C, radius, sigma, cfg, shared_kp_data=None, 
                           conv_layer=False, drop_path=-1):

        attention_groups = cfg.model.inv_groups
        if conv_layer or 'kpconvd' in self.kp_mode:
            attention_groups = 0

        #TMP to get 
        if self.kp_mode == 'kpconv':

            return KPConvResidualBlock(in_C,
                                       out_C,
                                       cfg.model.shell_sizes,
                                       radius,
                                       sigma,
                                       groups=cfg.model.conv_groups,
                                       shared_kp_data=shared_kp_data,
                                       influence_mode=cfg.model.kp_influence,
                                       dimension=cfg.data.dim,
                                       norm_type=cfg.model.norm,
                                       bn_momentum=cfg.model.bn_momentum)
        elif self.kp_mode == 'kpconvtest':
            return KPNextResidualBlock(in_C,
                                       out_C,
                                       cfg.model.shell_sizes,
                                       radius,
                                       sigma,
                                       attention_groups=attention_groups,
                                       attention_act=cfg.model.inv_act,
                                       mod_grp_norm=cfg.model.inv_grp_norm,
                                       shared_kp_data=shared_kp_data,
                                       influence_mode=cfg.model.kp_influence,
                                       dimension=cfg.data.dim,
                                       norm_type=cfg.model.norm,
                                       bn_momentum=cfg.model.bn_momentum)

        else:
            return KPNextMultiShortcutBlock(in_C,
                                            out_C,
                                            cfg.model.shell_sizes,
                                            radius,
                                            sigma,
                                            attention_groups=attention_groups,
                                            attention_act=cfg.model.inv_act,
                                            mod_grp_norm=cfg.model.inv_grp_norm,
                                            expansion=4,
                                            drop_path_p=drop_path,
                                            layer_scale_init_v=-1.,
                                            use_upcut=cfg.model.kpx_upcut,
                                            shared_kp_data=shared_kp_data,
                                            influence_mode=cfg.model.kp_influence,
                                            dimension=cfg.data.dim,
                                            norm_type=cfg.model.norm,
                                            bn_momentum=cfg.model.bn_momentum)






    def forward(self, batch, verbose=False):

        #  ------ Init ------
        
        if verbose:
            torch.cuda.synchronize(batch.device())
            t = [time.time()]

        # First complete the input pyramid if not already done
        if len(batch.in_dict.neighbors) < 1:
            fill_pyramid(batch.in_dict,
                         self.num_layers,
                         self.subsample_size,
                         self.first_radius,
                         self.radius_scaling,
                         self.neighbor_limits,
                         self.upsample_n,
                         sub_mode=self.in_sub_mode,
                         grid_pool_mode=self.grid_pool)

        if verbose: 
            torch.cuda.synchronize(batch.device())                           
            t += [time.time()]

        # Get input features
        feats = batch.in_dict.features.clone().detach()
        
        if verbose:      
            torch.cuda.synchronize(batch.device())                        
            t += [time.time()]

        
        #  ------ Stem ------
        feats = self.stem(batch.in_dict.points[0], batch.in_dict.points[0], feats, batch.in_dict.neighbors[0])
        # feats = self.stem(feats)


        #  ------ Encoder ------

        skip_feats = []
        for layer in range(1, self.num_layers + 1):

            # Get layer blocks
            l = layer -1
            block_list = getattr(self, 'encoder_{:d}'.format(layer))
            
            # Layer blocks
            if self.kp_mode in ['kpconv', 'kpconvtest']:
                for block in block_list:
                    feats = block(batch.in_dict.points[l], batch.in_dict.points[l], feats, batch.in_dict.neighbors[l])
            else:
                upcut = None
                for block in block_list:
                    feats, upcut = block(batch.in_dict.points[l], batch.in_dict.points[l], feats, batch.in_dict.neighbors[l], batch.in_dict.lengths[l], upcut=upcut)
                
            if layer < self.num_layers:

                # Skip features
                skip_feats.append(feats)

                # Pooling
                layer_pool = getattr(self, 'pooling_{:d}'.format(layer))
                if self.grid_pool:
                    feats = layer_pool(feats, batch.in_dict.pools[l])
                else:
                    feats = layer_pool(batch.in_dict.points[l+1], batch.in_dict.points[l], feats, batch.in_dict.pools[l])

         
        if verbose:    
            torch.cuda.synchronize(batch.device())                         
            t += [time.time()]

        if self.task == 'classification':
            
            # Global pooling
            feats = self.global_pooling(feats, batch.in_dict.lengths[-1])

            
        elif self.task == 'cloud_segmentation':

            #  ------ Decoder ------

            for layer in range(self.num_layers - 1, 0, -1):

                # Get layer blocks
                l = layer -1    # 3, 2, 1, 0
                upsample = getattr(self, 'upsampling_{:d}'.format(layer))

                # Upsample
                if self.grid_pool:
                    feats = upsample(feats, batch.in_dict.upsamples[l])
                else:
                    feats = upsample(feats, batch.in_dict.upsamples[l], batch.in_dict.up_distances[l])

                # Concat with skip features
                feats = torch.cat([feats, skip_feats[l]], dim=1)
                
                # MLP
                unary = getattr(self, 'decoder_unary_{:d}'.format(layer))
                feats = unary(feats)

                # Optional Decoder layers
                if self.add_decoder_layer:
                    block = getattr(self, 'decoder_layer_{:d}'.format(layer))
                    if self.kp_mode in ['kpconv', 'kpconvtest']:
                        feats = block(batch.in_dict.points[l], batch.in_dict.points[l], feats, batch.in_dict.neighbors[l])
                    else:
                        feats, _ = block(batch.in_dict.points[l], batch.in_dict.points[l], feats, batch.in_dict.neighbors[l], batch.in_dict.lengths[l])

        #  ------ Head ------

        logits = self.head(feats)
                

        if verbose:
            torch.cuda.synchronize(batch.device())                      
            t += [time.time()]
            mean_dt = 1000 * (np.array(t[1:]) - np.array(t[:-1]))
            message = ' ' * 75 + 'net (ms):'
            for dt in mean_dt:
                message += ' {:5.1f}'.format(dt)
            print(message)

        return logits

    def loss(self, outputs, labels):
        """
        Runs the loss on outputs of the model
        :param outputs: logits
        :param labels: labels
        :return: loss
        """

        # Set all ignored labels to -1 and correct the other label to be in [0, C-1] range
        target = - torch.ones_like(labels)
        for i, c in enumerate(self.valid_labels):
            target[labels == c] = i

        # Reshape to have size [1, C, N]
        outputs = torch.transpose(outputs, 0, 1)
        outputs = outputs.unsqueeze(0)
        target = target.squeeze().unsqueeze(0)

        # Cross entropy loss
        self.output_loss = self.criterion(outputs, target)

        # Combined loss
        return self.output_loss

    def loss_rsmix(self, outputs, labels, labels_b, lam):
        """
        Runs the loss on outputs of the model
        :param outputs: logits
        :param labels: labels
        :return: loss
        """

        B = int(outputs.shape[0])
        loss = 0
        for i in range(B):

            loss_a = self.loss(outputs[i].unsqueeze(0), labels[i].unsqueeze(0))
            loss_b = self.loss(outputs[i].unsqueeze(0), labels_b[i].unsqueeze(0))
            loss += loss_a * (1-lam[i]) + loss_b * lam[i]

        self.output_loss = loss/B

        return self.output_loss

    def accuracy(self, outputs, labels):
        """
        Computes accuracy of the current batch
        :param outputs: logits predicted by the network
        :param labels: labels
        :return: accuracy value
        """

        # Set all ignored labels to -1 and correct the other label to be in [0, C-1] range
        target = - torch.ones_like(labels)
        for i, c in enumerate(self.valid_labels):
            target[labels == c] = i

        predicted = torch.argmax(outputs.data, dim=1)
        total = target.size(0)
        correct = (predicted == target).sum().item()

        return correct / total














