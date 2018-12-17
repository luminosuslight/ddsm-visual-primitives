# classification into (no cancer / benign / cancer)

import argparse
import os
import time
import random
from datetime import datetime
from PIL import Image
import numpy as np

import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torchnet
import torchvision.models as models
import torchvision.transforms as transforms
from torch.utils.data.sampler import SubsetRandomSampler
from munch import Munch
from tensorboardX import SummaryWriter
from torch.autograd import Variable


class DDSM(torch.utils.data.Dataset):
    def __init__(self, root, split, transform):
        self.root = root
        self.transform = transform

        image_list = []
        with open(os.path.join(root, '{}.txt'.format(split)), 'r') as f:
            for line in f.readlines():
                image_name, label = line.strip().split(' ')
                label = int(label)
                if label != 0 or random.random() < 1/20.0:
                    # we are using only 1/20th of the normal images to balance the dataset better
                    image_list.append((image_name, label))
        self.image_list = image_list

        weight = np.unique([label for _, label in self.image_list], return_counts=True)[1]
        self.weight = 1 / (weight / np.amin(weight))

    def __len__(self):
        return len(self.image_list)

    def __getitem__(self, idx):
        image_name, label = self.image_list[idx]
        image = Image.open(os.path.join(self.root, image_name))
        image = image.crop([0, 0, 224, 224])
        image = self.transform(image)
        return image, label


def accuracy(output, target):
    pred = output.max(1)[1]
    return 100.0 * target.eq(pred).float().mean()


def save_checkpoint(checkpoint_dir, state, epoch):
    file_path = os.path.join(checkpoint_dir, 'checkpoint_{:08d}.pth.tar'.format(epoch))
    torch.save(state, file_path)
    return file_path


def adjust_learning_rate(optimizer, epoch):
    lr = cfg.optimizer.lr
    for e in cfg.optimizer.lr_decay_epochs:
        if epoch >= e:
            lr *= 0.1
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.val = None
        self.avg = None
        self.sum = None
        self.count = None
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    accuracies = AverageMeter()
    auc0 = torchnet.meter.AUCMeter()
    auc1 = torchnet.meter.AUCMeter()
    auc2 = torchnet.meter.AUCMeter()
    model.train()
    end = time.time()
    for i, (input_data, target) in enumerate(train_loader):
        data_time.update(time.time() - end)

        target = target.cuda(async=True)
        input_var = Variable(input_data)
        target_var = Variable(target)

        output = model(input_var)
        loss = criterion(output, target_var)

        acc = accuracy(output.data, target)
        losses.update(loss.item(), input_data.size(0))
        accuracies.update(acc, input_data.size(0))
        prob = nn.Softmax(dim=1)(output)
        auc0.add(prob.data[:, 0], target.eq(0))
        auc1.add(prob.data[:, 1], target.eq(1))
        auc2.add(prob.data[:, 2], target.eq(2))

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        batch_time.update(time.time() - end)
        end = time.time()

        if i % cfg.training.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Accuracy {accuracy.val:.4f} ({accuracy.avg:.4f})'.format(
                   epoch, i, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=losses, accuracy=accuracies))

    return batch_time.avg, data_time.avg, losses.avg, accuracies.avg, auc0.value()[0], auc1.value()[0], auc2.value()[0]


def validate(val_loader, model, criterion):
    batch_time = AverageMeter()
    losses = AverageMeter()
    accuracies = AverageMeter()
    auc0 = torchnet.meter.AUCMeter()
    auc1 = torchnet.meter.AUCMeter()
    auc2 = torchnet.meter.AUCMeter()
    model.eval()
    end = time.time()

    for i, (input_data, target) in enumerate(val_loader):

        with torch.no_grad():
            target = target.cuda(async=True)
            input_var = Variable(input_data)
            target_var = Variable(target)

            output = model(input_var)
            loss = criterion(output, target_var)

        acc = accuracy(output.data, target)
        losses.update(loss.item(), input_data.size(0))
        accuracies.update(acc, input_data.size(0))
        prob = nn.Softmax(dim=1)(output)
        auc0.add(prob.data[:, 0], target.eq(0))
        auc1.add(prob.data[:, 1], target.eq(1))
        auc2.add(prob.data[:, 2], target.eq(2))

        batch_time.update(time.time() - end)
        end = time.time()

        if i % cfg.training.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Accuracy {accuracy.val:.4f} ({accuracy.avg:.4f})'.format(
                   i, len(val_loader), batch_time=batch_time, loss=losses, accuracy=accuracies))

    return batch_time.avg, losses.avg, accuracies.avg, auc0.value()[0], auc1.value()[0], auc2.value()[0]


def main():
    if cfg.training.resume is not None:
        log_dir = cfg.training.log_dir
        checkpoint_dir = os.path.dirname(cfg.training.resume)
    else:
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')
        log_dir = os.path.join(cfg.training.logs_dir, '{}_{}'.format(timestamp, cfg.training.experiment_name))
        checkpoint_dir = os.path.join(cfg.training.checkpoints_dir,
                                      '{}_{}'.format(timestamp, cfg.training.experiment_name))
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
    print('log_dir: {}'.format(log_dir))
    print('checkpoint_dir: {}'.format(checkpoint_dir))

    print("=> creating model '{}'".format(cfg.arch.model))
    model = models.__dict__[cfg.arch.model](pretrained=cfg.arch.pretrained)

    if cfg.arch.model.startswith('alexnet') or cfg.arch.model.startswith('vgg'):
        # noinspection PyProtectedMember
        model.classifier._modules['6'] = nn.Linear(4096, cfg.arch.num_classes)
    elif cfg.arch.model == 'inception_v3':
        model.aux_logits = False
        model.fc = nn.Linear(2048, cfg.arch.num_classes)
    elif cfg.arch.model == 'resnet152':
        model.fc = nn.Linear(2048, cfg.arch.num_classes)
    else:
        raise Exception

    if cfg.arch.model.startswith('alexnet') or cfg.arch.model.startswith('vgg'):
        model.features = torch.nn.DataParallel(model.features)
        model.cuda()
    else:
        model = torch.nn.DataParallel(model).cuda()
    cudnn.benchmark = True
    optimizer = torch.optim.SGD(model.parameters(),
                                lr=cfg.optimizer.lr,
                                momentum=cfg.optimizer.momentum,
                                weight_decay=cfg.optimizer.weight_decay)

    start_epoch = 0
    if cfg.training.resume is not None:
        if os.path.isfile(cfg.training.resume):
            print("=> loading checkpoint '{}'".format(cfg.training.resume))
            checkpoint = torch.load(cfg.training.resume)
            start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})".format(cfg.training.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(cfg.training.resume))
            print('')
            raise Exception

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    train_transforms = []
    val_transforms = []
    if cfg.arch.model == 'inception_v3':
        train_transforms.append(transforms.Scale(299))
        val_transforms.append(transforms.Scale(299))

    train_dataset = DDSM(cfg.data.root, 'train', transforms.Compose(train_transforms + [
        transforms.ToTensor(),
        normalize,
    ]))
    val_dataset = DDSM(cfg.data.root, 'val', transforms.Compose(val_transforms + [
        transforms.ToTensor(),
        normalize,
    ]))

    criterion = nn.CrossEntropyLoss(weight=torch.from_numpy(train_dataset.weight).float()).cuda()

    if cfg.training.debug:
        # limit training data for debugging:
        train_indices = range(cfg.data.batch_size * 10)

        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=cfg.data.batch_size, shuffle=False,
            num_workers=cfg.data.workers, pin_memory=True, sampler=SubsetRandomSampler(train_indices))
        val_loader = torch.utils.data.DataLoader(
            val_dataset, batch_size=cfg.data.batch_size, shuffle=False,
            num_workers=cfg.data.workers, pin_memory=True, sampler=SubsetRandomSampler(train_indices))
    else:
        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=cfg.data.batch_size, shuffle=True,
            num_workers=cfg.data.workers, pin_memory=True)
        val_loader = torch.utils.data.DataLoader(
            val_dataset, batch_size=cfg.data.batch_size, shuffle=False,
            num_workers=cfg.data.workers, pin_memory=True)

    train_summary_writer = SummaryWriter(log_dir=os.path.join(log_dir, 'train'))
    val_summary_writer = SummaryWriter(log_dir=os.path.join(log_dir, 'val'))
    for epoch in range(start_epoch, cfg.training.epochs):
        lr = adjust_learning_rate(optimizer, epoch)
        train_summary_writer.add_scalar('learning_rate', lr, epoch + 1)

        train_batch_time, train_data_time, train_loss, train_accuracy, train_auc0, train_auc1, train_auc2 = train(
            train_loader, model, criterion, optimizer, epoch)
        train_summary_writer.add_scalar('batch_time', train_batch_time, epoch + 1)
        train_summary_writer.add_scalar('loss', train_loss, epoch + 1)
        train_summary_writer.add_scalar('accuracy', train_accuracy, epoch + 1)
        train_summary_writer.add_scalar('auc0', train_auc0, epoch + 1)
        train_summary_writer.add_scalar('auc1', train_auc1, epoch + 1)
        train_summary_writer.add_scalar('auc2', train_auc2, epoch + 1)

        val_batch_time, val_loss, val_accuracy, val_auc0, val_auc1, val_auc2 = validate(
            val_loader, model, criterion)
        val_summary_writer.add_scalar('batch_time', val_batch_time, epoch + 1)
        val_summary_writer.add_scalar('loss', val_loss, epoch + 1)
        val_summary_writer.add_scalar('accuracy', val_accuracy, epoch + 1)
        val_summary_writer.add_scalar('auc0', val_auc0, epoch + 1)
        val_summary_writer.add_scalar('auc1', val_auc1, epoch + 1)
        val_summary_writer.add_scalar('auc2', val_auc2, epoch + 1)

        if (epoch + 1) % cfg.training.checkpoint_epochs == 0:
            checkpoint_path = save_checkpoint(checkpoint_dir, {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
            }, epoch + 1)
            cfg.training.log_dir = log_dir
            cfg.training.resume = checkpoint_path
            with open(os.path.join(log_dir, 'config.yml'), 'w') as f:
                f.write(cfg.toYAML())
            print("Checkpoint written: " + checkpoint_path)

    print("Training finished.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config_path', metavar='PATH', help='path to config file')
    args = parser.parse_args()
    config_path = args.config_path
    with open(config_path, 'r') as config_file:
        cfg = Munch.fromYAML(config_file)
    startTime = datetime.now()
    main()
    print("Runtime: %s" % (datetime.now() - startTime))
