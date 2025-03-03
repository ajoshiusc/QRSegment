import argparse
import logging
import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
import wandb
from torch import optim
from torch.utils.data import DataLoader, random_split, TensorDataset
from tqdm import tqdm

from utils.data_loading import BasicDataset, CarvanaDataset
from utils.dice_score import dice_loss
from evaluate import evaluate_grayscale_QR_4Q
from unet import QRUNet_4Q
import numpy as np

dir_img = Path('./data/imgs/')
dir_mask = Path('./data/masks/')
dir_checkpoint = Path('./checkpoints/')


Q1 = 0.875 # 0.9 #0.75
Q2 = 0.625 #0.8# 0.5
Q3 = 0.375 #0.75 #0.25
Q4 = 0.125



def BCEqr_W(P, Y, q):
    q= 166
    L = q*Y*torch.log2(P+1e-16) + (1.0-Y)*torch.log2(1.0-P+1e-16)

    return torch.sum(-L)

def BCEqr(P, Y, q):
    q= 0.5
    L = q*Y*torch.log2(P+1e-16) + (1.0-q)*(1.0-Y)*torch.log2(1.0-P+1e-16)

    return torch.sum(-L)

# This is the new cost function


def QRcost_new(f, Y, q=0.5):
    error = f - Y
    smaller_index = error < 0
    bigger_index = 0 < error
    loss = q * torch.sum(torch.abs(error)[smaller_index]) + (1-q) * torch.sum(torch.abs(error)[bigger_index])

    return torch.sum(loss)


def QRcost_warmup(f, Y, q=0.5, h=0.1):
    #L = (Y - (1-q))*torch.sigmoid((f-.5)/h)
    q=0.625
    L = (Y - (1.0-q))*(f)

    return torch.sum(-L)

def QRcost(f, Y, q=0.5, h=0.1):
    #L = (Y - (1-q))*torch.sigmoid((f-.5)/h)
    L = (Y - (1.0-q))*(f)

    return torch.sum(-L)


def train_net(net,
              device,
              epochs: int = 5,
              batch_size: int = 1,
              learning_rate: float = 1e-6, #0.001,
              val_percent: float = 0.1,
              save_checkpoint: bool = True,
              img_scale: float = 0.5,
              amp: bool = False):
    # 1. Create dataset

    d = np.load('/big_disk/ajoshi/LIDC_data/train_less_sub_500.npz')
    X = d['images']
    M = d['masks']
    X = np.expand_dims(X, axis=3)
    M = np.expand_dims(M, axis=3)

    X = np.concatenate((X, M), axis=3)

    # 2. Split into train / validation partitions
    n_val = int(len(X) * val_percent)
    n_train = len(X) - n_val
    train_set, val_set = random_split(
        X, [n_train, n_val], generator=torch.Generator().manual_seed(0))

    # 3. Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=False, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False,
                            drop_last=True, **loader_args)

    # (Initialize logging)
    experiment = wandb.init(project='U-Net', resume='allow', anonymous='must')
    experiment.config.update(dict(epochs=epochs, batch_size=batch_size, learning_rate=learning_rate,
                                  val_percent=val_percent, save_checkpoint=save_checkpoint, img_scale=img_scale,
                                  amp=amp))

    logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {learning_rate}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_checkpoint}
        Device:          {device.type}
        Images scaling:  {img_scale}
        Mixed Precision: {amp}
    ''')

    # 4. Set up the optimizer, the loss, the learning rate scheduler and the loss scaling for AMP
    optimizer = optim.RMSprop(
        net.parameters(), lr=learning_rate, weight_decay=1e-8, momentum=0.9)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, 'max', patience=2)  # goal: maximize Dice score
    grad_scaler = torch.cuda.amp.GradScaler(enabled=amp)
    # BCEqr #nn.BCELoss(reduction='sum')  #nn.CrossEntropyLoss()
    #criterion = QRcost # BCEqr #
    global_step = 0

    # 5. Begin training
    for epoch in range(epochs):

        if epoch<1:
            criterion = BCEqr_W
        else:
            criterion = QRcost#QRcost

        net.train()
        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images = batch[:, :, :, np.newaxis, 0].permute(
                    (0, 3, 1, 2))  # ['image']
                true_masks = batch[:, :, :, 1]  # batch['mask']

                assert images.shape[1] == net.n_channels, \
                    f'Network has been defined with {net.n_channels} input channels, ' \
                    f'but loaded images have {images.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32)
                true_masks = true_masks.to(device=device, dtype=torch.float32)

                with torch.cuda.amp.autocast(enabled=amp):
                    masks_pred1, masks_pred2, masks_pred3,masks_pred4 = net(images)
                    loss = criterion(masks_pred1[:, 1, ], true_masks[:, ], q=Q1) + criterion(
                        masks_pred2[:, 1, ], true_masks[:, ], q=Q2) + criterion(masks_pred3[:, 1, ], true_masks[:, ], q=Q3)+  criterion(masks_pred4[:, 1, ], true_masks[:, ], q=Q4)  # \
                    # + dice_loss(F.softmax(masks_pred, dim=1).float(),
                    #             F.one_hot(true_masks, net.n_classes).permute(0, 3, 1, 2).float(),
                    #             multiclass=True)

                optimizer.zero_grad(set_to_none=True)
                grad_scaler.scale(loss).backward()
                grad_scaler.step(optimizer)
                grad_scaler.update()

                pbar.update(images.shape[0])
                global_step += 1
                epoch_loss += loss.item()
                experiment.log({
                    'train loss': loss.item(),
                    'step': global_step,
                    'epoch': epoch
                })
                pbar.set_postfix(**{'loss (batch)': loss.item()})

                # Evaluation round
                if global_step % (n_train // (10 * batch_size)) == 0:
                    histograms = {}
                    for tag, value in net.named_parameters():
                        tag = tag.replace('/', '.')
                        histograms['Weights/' +
                                   tag] = wandb.Histogram(value.data.cpu())
                        histograms['Gradients/' +
                                   tag] = wandb.Histogram(value.grad.data.cpu())

                    val_score = evaluate_grayscale_QR_4Q(net, val_loader, device)
                    scheduler.step(val_score)

                    logging.info('Validation Dice score: {}'.format(val_score))
                    experiment.log({
                        'learning rate': optimizer.param_groups[0]['lr'],
                        'validation Dice': val_score,
                        'images': wandb.Image(images[0, 0].cpu()),
                        'masks': {
                            'true': wandb.Image(true_masks[0].float().cpu()),
                            'pred1': wandb.Image((masks_pred1[0, 1] > 0.5).float().cpu()),
                            'pred2': wandb.Image((masks_pred2[0, 1] > 0.5).float().cpu()),
                            'pred3': wandb.Image((masks_pred3[0, 1] > 0.5).float().cpu()),
                            'pred4': wandb.Image((masks_pred4[0, 1] > 0.5).float().cpu()),
                        },
                        'step': global_step,
                        'epoch': epoch,
                        **histograms
                    })

 
        if epoch == 0:
            torch.save(net.state_dict(), 'LIDC_4Q_QR_0.pth')
 
        if save_checkpoint:
            Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), str(dir_checkpoint /
                       'checkpoint_epoch{}.pth'.format(epoch + 1)))
            logging.info(f'Checkpoint {epoch + 1} saved!')


def get_args():
    parser = argparse.ArgumentParser(
        description='Train the UNet on images and target masks')
    parser.add_argument('--epochs', '-e', metavar='E',
                        type=int, default=20, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size',
                        metavar='B', type=int, default=40, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=0.00001,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str,
                        default=False, help='Load model from a .pth file')
    parser.add_argument('--scale', '-s', type=float,
                        default=0.5, help='Downscaling factor of the images')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--amp', action='store_true',
                        default=False, help='Use mixed precision')

    return parser.parse_args()


if __name__ == '__main__':
    args = get_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(levelname)s: %(message)s')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    net = QRUNet_4Q(n_channels=1, n_classes=2, bilinear=True)

    logging.info(f'Network:\n'
                 f'\t{net.n_channels} input channels\n'
                 f'\t{net.n_classes} output channels (classes)\n'
                 f'\t{"Bilinear" if net.bilinear else "Transposed conv"} upscaling')

    if args.load:
        net.load_state_dict(torch.load(args.load, map_location=device))
        logging.info(f'Model loaded from {args.load}')

    net.to(device=device)
    try:
        train_net(net=net,
                  epochs=args.epochs,
                  batch_size=args.batch_size,
                  learning_rate=args.lr,
                  device=device,
                  img_scale=args.scale,
                  val_percent=args.val / 100,
                  amp=args.amp)
        torch.save(net.state_dict(), 'LIDC_4Q_QR_500.pth')
    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'INTERRUPTED.pth')
        logging.info('Saved interrupt')
        sys.exit(0)
