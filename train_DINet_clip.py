from models.Discriminator import Discriminator
from models.VGG19 import Vgg19
from models.DINet import DINet
from models.Syncnet import SyncNetPerception
from utils.training_utils import get_scheduler, update_learning_rate,GANLoss
from config.config import DINetTrainingOptions
from sync_batchnorm import convert_model
from torch.utils.data import DataLoader
from dataset.dataset_DINet_clip import DINetDataset

#from torchmetrics.image import MultiScaleStructuralSimilarityIndexMeasure


import random
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import os
import torch.nn.functional as F

import loralib as lora

import glob
import cv2
import csv
from utils.data_processing import load_landmark_openface,compute_crop_radius

from torch.utils.tensorboard import SummaryWriter
writer = SummaryWriter()

def blending (fake_out, source_clip, train_data):
    
    fake_out = fake_out.cpu().detach().numpy()
    source_clip = source_clip.cpu().detach().numpy()

    result_clip = []
    
    for out, clip in zip(fake_out,source_clip):

        out = np.transpose(out, (1,2,0))
        clip = np.transpose(clip, (1,2,0))
        
        mask = np.zeros_like(clip)
        mask[train_data.radius + train_data.mouth_region_size//6 : train_data.mouth_region_size + train_data.radius//4,
        train_data.radius_1_4 + train_data.mouth_region_size//6 : train_data.mouth_region_size + train_data.radius_1_4//4  , :] = 1

        mask = cv2.blur(mask, (21,21))
        result = mask * out + (1-mask) * clip

        result_clip.append(np.transpose(result, (2, 0, 1)))
        
        #cv2.imshow("fake_out", cv2.cvtColor(out , cv2.COLOR_BGR2RGB))
        #cv2.imshow("source_clip", cv2.cvtColor(clip , cv2.COLOR_BGR2RGB))
        #cv2.imshow("source_image_mask", mask)
        #cv2.imshow("result", result)

        #cv2.waitKey(0)
        #cv2.destroyAllWindows()
    
    return torch.tensor(result_clip).cuda()

if __name__ == "__main__":
    '''
            clip training code of DINet
            in the resolution you want, using clip training code after frame training
            
        '''
    # load config
    opt = DINetTrainingOptions().parse_args()
    random.seed(opt.seed)
    np.random.seed(opt.seed)
    torch.cuda.manual_seed(opt.seed)
    # load training data
    train_data = DINetDataset(opt.train_data, opt.train_data_org, opt.augment_num,opt.mouth_region_size)
    #train_data_org = DINetDataset(opt.train_data_org,opt.augment_num,opt.mouth_region_size)

    #print(train_data)
    #print(train_data_org)
    training_data_loader = DataLoader(dataset=train_data,  batch_size=opt.batch_size, shuffle=True,drop_last=True)
    train_data_length = len(training_data_loader)


    # load original training data
    #train_data_org = DINetDataset(opt.train_data_org,opt.augment_num,opt.mouth_region_size)
    #training_data_loader_org = DataLoader(dataset=train_data_org,  batch_size=opt.batch_size, shuffle=True,drop_last=True)
    #train_data_length_org = len(training_data_loader_org)


    # init network
    net_g = DINet(opt.source_channel,opt.ref_channel,opt.audio_channel).cuda()
    net_dI = Discriminator(opt.source_channel ,opt.D_block_expansion, opt.D_num_blocks, opt.D_max_features).cuda()
    net_dV = Discriminator(opt.source_channel * 5, opt.D_block_expansion, opt.D_num_blocks, opt.D_max_features).cuda()
    net_vgg = Vgg19().cuda()
    net_lipsync = SyncNetPerception(opt.pretrained_syncnet_path).cuda()
    # parallel
    net_g = nn.DataParallel(net_g)
    net_g = convert_model(net_g)
    net_dI = nn.DataParallel(net_dI)
    net_dV = nn.DataParallel(net_dV)
    net_vgg = nn.DataParallel(net_vgg)
    # setup optimizer
    optimizer_g = optim.AdamW(net_g.parameters(), lr=opt.lr_g, weight_decay=0)
    optimizer_dI = optim.AdamW(net_dI.parameters(), lr=opt.lr_dI, weight_decay=0)
    optimizer_dV = optim.AdamW(net_dV.parameters(), lr=opt.lr_dI, weight_decay=0)
    ## load frame trained DInet weight
    print('loading frame trained DINet weight from: {}'.format(opt.pretrained_frame_DINet_path))

    checkpoint = torch.load(opt.pretrained_frame_DINet_path)

    net_g.load_state_dict(checkpoint['state_dict']['net_g'], strict=False)
    net_dI.load_state_dict(checkpoint['state_dict']['net_dI'], strict=False)
    net_dV.load_state_dict(checkpoint['state_dict']['net_dV'], strict=False)

    #optimizer_g.load_state_dict(checkpoint['optimizer']['net_g'])
    #optimizer_dI.load_state_dict(checkpoint['optimizer']['net_dI'])
    #optimizer_dV.load_state_dict(checkpoint['optimizer']['net_dV'])
    # set criterion
    criterionGAN = GANLoss().cuda()
    criterionL1 = nn.L1Loss().cuda()
    criterionMSE = nn.MSELoss().cuda()
    #criterionSSIM = MultiScaleStructuralSimilarityIndexMeasure(kernel_size= 5,data_range=1.0).cuda()


    #iter_range = opt.start_epoch // train_data_length

    #opt.non_decay = opt.start_epoch//3 
    #opt.decay = opt.start_epoch - opt.non_decay

    print( opt.non_decay, opt.decay)

    
    # set scheduler
    net_g_scheduler = get_scheduler(optimizer_g, opt.non_decay, opt.decay, lr_policy = 'cosine')
    net_dI_scheduler = get_scheduler(optimizer_dI, opt.non_decay, opt.decay, lr_policy = 'cosine')
    net_dV_scheduler = get_scheduler(optimizer_dV, opt.non_decay, opt.decay, lr_policy = 'cosine')
    # set label of syncnet perception loss
    real_tensor = torch.tensor(1.0).cuda()

    """
    for name, param in net_g.named_parameters():
        if not "out_conv" in name:
            param.requires_grad = False
        #print(name)
    
    for name, param in net_dI.named_parameters():
            param.requires_grad = False
    
    for name, param in net_dV.named_parameters():
            param.requires_grad = False
    """
    universal_checkpoint = 0
    #print(f"{iter_range} epoch will be trained the model!")

    print(train_data.length)

    # start train
    for epoch in range(opt.start_epoch, opt.non_decay+opt.decay+1):
        #epoch+=1
        net_g.train()
        lora.mark_only_lora_as_trainable(net_g)

        for iteration, data in enumerate(training_data_loader):
            # forward
            source_clip,source_clip_mask, reference_clip,deep_speech_clip,deep_speech_full = data
            #print(source_clip.shape)
            #print(reference_clip.shape)
            #print(deep_speech_clip.shape)
            #print("--------------------------------")
            source_clip = torch.cat(torch.split(source_clip, 1, dim=1), 0).squeeze(1).float().cuda()
            #frame_clip = torch.cat(torch.split(frame_clip, 1, dim=1), 0).squeeze(1).float().cuda()
            #landmark = torch.cat(torch.split(landmark, 1, dim=1), 0).squeeze(1).float().cuda()
            writer.add_images("real/train", source_clip, global_step=universal_checkpoint)
            source_clip_mask = torch.cat(torch.split(source_clip_mask, 1, dim=1), 0).squeeze(1).float().cuda()
            reference_clip = torch.cat(torch.split(reference_clip, 1, dim=1), 0).squeeze(1).float().cuda()
            
            deep_speech_clip = torch.cat(torch.split(deep_speech_clip, 1, dim=1), 0).squeeze(1).float().cuda()
            deep_speech_full = deep_speech_full.float().cuda()
            fake_out = net_g(source_clip_mask,reference_clip,deep_speech_clip)

            blended_clip = blending(fake_out, source_clip, train_data)
            blended_clip_half = F.interpolate(blended_clip, scale_factor=0.5, mode='bilinear')

            fake_out_half = F.avg_pool2d(fake_out, 3, 2, 1, count_include_pad=False)
            source_clip_half = F.interpolate(source_clip, scale_factor=0.5, mode='bilinear')
            writer.add_images("generated/1-Input", source_clip, global_step=universal_checkpoint)
            writer.add_images("generated/2-MaskedInput", source_clip_mask, global_step=universal_checkpoint)
            writer.add_images("generated/3-Fake", fake_out, global_step=universal_checkpoint)
            # (1) Update DI network
            optimizer_dI.zero_grad()
            _,pred_fake_dI = net_dI(fake_out)
            loss_dI_fake = criterionGAN(pred_fake_dI, False)
            _,pred_real_dI = net_dI(source_clip)
            loss_dI_real = criterionGAN(pred_real_dI, True)
            # Combined DI loss
            loss_dI = (loss_dI_fake + loss_dI_real) * 0.5
            loss_dI.backward(retain_graph=True)
            optimizer_dI.step()

            # (2) Update DV network
            optimizer_dV.zero_grad()
            condition_fake_dV = torch.cat(torch.split(fake_out, opt.batch_size, dim=0), 1)
            _, pred_fake_dV = net_dV(condition_fake_dV)
            loss_dV_fake = criterionGAN(pred_fake_dV, False)
            condition_real_dV = torch.cat(torch.split(source_clip, opt.batch_size, dim=0), 1)
            _, pred_real_dV = net_dV(condition_real_dV)
            loss_dV_real = criterionGAN(pred_real_dV, True)
            # Combined DV loss
            loss_dV = (loss_dV_fake + loss_dV_real) * 0.5
            loss_dV.backward(retain_graph=True)
            optimizer_dV.step()

            # (2) Update DINet
            _, pred_fake_dI = net_dI(fake_out)
            _, pred_fake_dV = net_dV(condition_fake_dV)
            optimizer_g.zero_grad()
            # compute perception loss
            perception_real = net_vgg(source_clip)
            perception_fake = net_vgg(fake_out)

            perception_blended = net_vgg(blended_clip)
            perception_blended_half = net_vgg(blended_clip_half)
          
            perception_real_half = net_vgg(source_clip_half)
            perception_fake_half = net_vgg(fake_out_half)


            loss_g_perception = 0
            for i in range(len(perception_real)):
                loss_g_perception += criterionL1(perception_fake[i], perception_real[i])
                loss_g_perception += criterionL1(perception_fake_half[i], perception_real_half[i])

            loss_g_perception_blend = 0
            for i in range(len(perception_real)):
                loss_g_perception_blend += criterionL1(perception_blended[i], perception_real[i])
                loss_g_perception_blend += criterionL1(perception_blended_half[i], perception_real_half[i])
            
            loss_g_perception = (loss_g_perception*0.6 + loss_g_perception_blend*0.4/ (len(perception_real) * 2)) * opt.lamb_perception
       
            # # gan dI loss
            loss_g_dI = criterionGAN(pred_fake_dI, True)
            # # gan dV loss
            loss_g_dV = criterionGAN(pred_fake_dV, True)
            ## sync perception loss
            fake_out_clip = torch.cat(torch.split(fake_out, opt.batch_size, dim=0), 1)
            blend_out_clip = torch.cat(torch.split(blended_clip, opt.batch_size, dim=0), 1)

            fake_out_clip_mouth = fake_out_clip[:, :, train_data.radius:train_data.radius + train_data.mouth_region_size,
            train_data.radius_1_4:train_data.radius_1_4 + train_data.mouth_region_size]
            sync_score = net_lipsync(fake_out_clip_mouth, deep_speech_full)

            loss_sync = criterionMSE(sync_score, real_tensor.expand_as(sync_score))* opt.lamb_syncnet_perception
    
            # combine all losses
            loss_g =   loss_g_perception + loss_g_dI +loss_g_dV + loss_sync
            loss_g.backward()
            optimizer_g.step()

            writer.add_scalar("Loss_DI", float(loss_dI), universal_checkpoint)
            writer.add_scalar("Loss_GI", float(loss_g_dI), universal_checkpoint)
            writer.add_scalar("Loss_DV", float(loss_dV), universal_checkpoint)
            writer.add_scalar("Loss_GV", float(loss_g_dV), universal_checkpoint)
            writer.add_scalar("Train/Loss_perception", float(loss_g_perception), universal_checkpoint)
            writer.add_scalar("Train/Loss_sync", float(loss_sync), universal_checkpoint)
            writer.add_scalar("Train/Loss_g", float(loss_g), universal_checkpoint)

            print(
                "===> Epoch[{}]({}/{}):  Loss_DI: {:.4f} Loss_GI: {:.4f} Loss_DV: {:.4f} Loss_GV: {:.4f} Loss_perception: {:.4f} Loss_sync: {:.4f} lr_g = {:.7f} ".format(
                    epoch, iteration, len(training_data_loader), float(loss_dI), float(loss_g_dI),float(loss_dV), float(loss_g_dV), float(loss_g_perception),float(loss_sync),
                    optimizer_g.param_groups[0]['lr']))

            universal_checkpoint+=1

           
        update_learning_rate(net_g_scheduler, optimizer_g)
        update_learning_rate(net_dI_scheduler, optimizer_dI)
        update_learning_rate(net_dV_scheduler, optimizer_dV)
        # checkpoint
        if epoch %  opt.checkpoint == 0:
            if not os.path.exists(opt.result_path):
                os.mkdir(opt.result_path)
            model_out_path = os.path.join(opt.result_path, 'netG_model_epoch_{}.pth'.format(epoch))
            states = {
                'epoch': epoch + 1,
                'state_dict': {'net_g': net_g.state_dict(),'net_dI': net_dI.state_dict(),'net_dV': net_dV.state_dict()},
                'optimizer': {'net_g': optimizer_g.state_dict(), 'net_dI': optimizer_dI.state_dict(), 'net_dV': optimizer_dV.state_dict()}
            }
            torch.save(states, model_out_path)
            #torch.save(lora.lora_state_dict(net_g.state_dict()), model_out_path.split(".p")[0]+"_lora.pth")
            print("Checkpoint saved to {}".format(epoch))
