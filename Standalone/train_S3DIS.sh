#!/bin/bash
#
# For licensing see accompanying LICENSE file.
# Copyright (C) 2024 Apple Inc. All Rights Reserved.
#

# Go to the root directory
cd KPConvX
export PYTHONPATH=$PWD:$PYTHONPATH


############
# Parameters
############

# You can change the path to your dataset here
ARGS="--dataset_path $PWD/../data/s3dis"

# Here you can define arguments to change network/training parameters.
# For example:
# ARGS="$ARGS --in_radius 1.5"
# ARGS="$ARGS --layer_blocks 3 3 9 12 3"
# ARGS="$ARGS --batch_size 32 --accum_batch 2"
# Have a look at the train.py file to see all available parameters. 
# You can also change the values of these parameters directly in the train.py file.


###############
# Main function
###############

# Define the experiment and script to run
EXP="S3DIS"
SCRIPT="train_S3DIS.py"

# Start the training
python3 experiments/$EXP/$SCRIPT $ARGS

