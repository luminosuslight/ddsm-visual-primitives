import os

import torch
import torch.utils.data
import torch.backends.cudnn as cudnn
import torch.nn as nn

from resnet import resnet152


def get_resnet152_3class_model(checkpoint_path=None):
    print("=> creating model 'resnet152'")
    model = resnet152(pretrained=not checkpoint_path)
    model.fc = nn.Linear(2048, 3)
    features_layer = model.layer4

    model = torch.nn.DataParallel(model).cuda()
    cudnn.benchmark = True

    epoch = 0
    optimizer_state = None
    if checkpoint_path and os.path.isfile(checkpoint_path):
        print("=> loading checkpoint '{}'".format(checkpoint_path))
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint['state_dict'])
        model.eval()
        optimizer_state = checkpoint['optimizer']
        epoch = checkpoint['epoch']
        print("=> loaded checkpoint '{}' (epoch {})".format(checkpoint_path, epoch))
    else:
        print("=> no checkpoint loaded, only ImageNet weights")

    return model, epoch, optimizer_state, features_layer
