import argparse
import logging
import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import optim
from torch.utils.data import DataLoader, random_split, TensorDataset
from tqdm import tqdm

from util.data_loading import BasicDataset, CarvanaDataset
from util.dice_score import dice_loss
from evaluate import evaluate_grayscale_QR_prob
import numpy as np
import torch
import numpy as np
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from probabilistic_QRunet import ProbabilisticQRUnet
from utils import l2_regularisation



dir_checkpoint = Path('./checkpoints_LIDC_QR_prob_unet/')


def train_net(net,
              device,
              epochs: int = 5,
              batch_size: int = 1,
              learning_rate: float = 1e-4, #0.001,
              val_percent: float = 0.1,
              save_checkpoint: bool = True,
              img_scale: float = 0.5,
              amp: bool = False):
    # 1. Create dataset

    d = np.load = np.load('cone_data_sim_training30000.npz')
    X = d['data'] 
    X=X/(X.max()+1e-4)
    M = d['masks']
    X = X[:,::2,::2]
    M = M[:,::2,::2]
    X = np.expand_dims(X, axis=3)
    M = np.expand_dims(M, axis=3)

    X = np.concatenate((X, M), axis=3)

    # 2. Split into train / validation partitions
    n_val = int(len(X) * val_percent)
    n_train = len(X) - n_val
    train_set, val_set = random_split(X, [n_train, n_val])

    # 3. Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=False, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False,
                            drop_last=True, **loader_args)

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
    optimizer = torch.optim.SGD(net.parameters(), lr=1e-4, weight_decay=0)
    grad_scaler = torch.cuda.amp.GradScaler(enabled=amp)
    # BCEqr #nn.BCELoss(reduction='sum')  #nn.CrossEntropyLoss()
    #criterion = QRcost # BCEqr #
    global_step = 0

    # 5. Begin training
    for epoch in range(epochs):

        net.train()
        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images = batch[:, :, :, np.newaxis, 0].permute(
                    (0, 3, 1, 2))  # ['image']
                true_masks = batch[:, :, :, 1] # batch['mask']


                #assert images.shape[1] == net.n_channels, \
                    #f'Network has been defined with {net.n_channels} input channels, ' \
                    #f'but loaded images have {images.shape[1]} channels. Please check that ' \
                   # 'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32)
                true_masks = true_masks.to(device=device, dtype=torch.float32)

                true_masks = torch.unsqueeze(true_masks,1)
                net.forward(images, true_masks, training=True)
                #masks_pred1=(F.sigmoid(net.sample(testing=True)) > 0.5).float()
                elbo = net.elbo(true_masks, epoch=10)
                reg_loss = l2_regularisation(net.posterior) + l2_regularisation(net.prior) + l2_regularisation(net.fcomb.layers)
                loss = -elbo + 1e-5 * reg_loss
                optimizer.zero_grad()
                if loss==loss:
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(net.parameters(), max_norm=15)

                    optimizer.step()
                
                    # + dice_loss(F.softmax(masks_pred, dim=1).float(),
                    #             F.one_hot(true_masks, net.n_classes).permute(0, 3, 1, 2).float(),
                    #             multiclass=True)

                """                 
                optimizer.zero_grad(set_to_none=True)
                grad_scaler.scale(loss).backward()
                grad_scaler.step(optimizer)
                grad_scaler.update()
                """
                pbar.update(images.shape[0])
                global_step += 1
                epoch_loss += loss.item()
                pbar.set_postfix(**{'loss (batch)': loss.item()})

                # Evaluation round
                if global_step % (n_train // (10 * batch_size)) == 0:
                    histograms = {}
                    for tag, value in net.named_parameters():
                        tag = tag.replace('/', '.')

                    val_score = evaluate_grayscale_QR_prob(net, val_loader, device)
                    #scheduler.step(val_score)

                    logging.info('Validation Dice score: {}'.format(val_score))

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
                        metavar='B', type=int, default=24, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=1e-6,
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
    torch.manual_seed(11)

    logging.basicConfig(level=logging.INFO,
                        format='%(levelname)s: %(message)s')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    net = ProbabilisticQRUnet(input_channels=1, num_classes=1, num_filters=[32,64,128,192], latent_dim=2, no_convs_fcomb=4, beta=10.0)



    #logging.info(f'Network:\n'
                 #f'\t{net.n_channels} input channels\n'
                # f'\t{net.n_classes} output channels (classes)\n'
                 #f'\t{"Bilinear" if net.bilinear else "Transposed conv"} upscaling')

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
        torch.save(net.state_dict(), 'LIDC_QR_prob_'+str(args.epochs)+'_cones_bceqr.pth')
    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'LIDC_QR_prob_INTERRUPTED_cones_bceqr.pth')
        logging.info('Saved interrupt')
        sys.exit(0)
