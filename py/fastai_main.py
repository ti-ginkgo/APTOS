import os
import sys
import gc
import time
import random
import cv2
import glob
import requests
import json
import math
import re
import hashlib

import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt

from functools import partial
from collections import Counter
from PIL import Image
from multiprocessing import cpu_count
from tqdm import tqdm_notebook as tqdm
from multiprocessing import Pool

import torch
import torch.nn as nn
import torch.nn.init as init
import torch.optim as optim
import torch.nn.functional as F
import torchvision.models as models
import torchvision.transforms as transforms
from torch.utils.data import DataLoader, Dataset, SubsetRandomSampler

from fastai import *
from fastai.vision import *
from fastai.callbacks import *

from sklearn.preprocessing import LabelEncoder, OneHotEncoder
from sklearn.model_selection import train_test_split
from sklearn.metrics import cohen_kappa_score

import albumentations as A
from albumentations import torch as AT

def get_df():
    base_image_dir = os.path.join('..', 'input', 'aptos2019-blindness-detection')
    train_dir = os.path.join(base_image_dir, 'train_images')
    df = pd.read_csv(os.path.join(base_image_dir, 'train.csv'))
    df['path'] = df['id_code'].map(lambda x: os.path.join(train_dir,'{}.png'.format(x)))
    df = df.drop(columns=['id_code'])
    df = df.sample(frac=1).reset_index(drop=True) 
    test_df = pd.read_csv(os.path.join(base_image_dir, 'sample_submission.csv'))
    return df, test_df

def get_old_df():
    base_image_dir = os.path.join('..', 'input', 'diabetic-retinopathy-resized')
    train_dir = os.path.join(base_image_dir, 'resized_train_cropped', 'resized_train_cropped')
    df = pd.read_csv(os.path.join(base_image_dir, 'trainLabels_cropped.csv'))
    df['path'] = df['image'].map(lambda x: os.path.join(train_dir,'{}.jpeg'.format(x)))
    df = df.drop(columns=['image'])
    df = df.sample(frac=1).reset_index(drop=True) 
    df = df.rename(columns={'level': 'diagnosis'})
    df.drop(['Unnamed: 0', 'Unnamed: 0.1'], inplace=True, axis=1)
    return df

def qk(y_pred, y):
    return torch.tensor(cohen_kappa_score(torch.round(y_pred), y, weights='quadratic'), device='cuda:0')

def seed_everything():
    seed = 43
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True

def main():
    seed_everything()

    df, test_df = get_df()
    old_df = get_old_df()

    model = EfficientNet.from_pretrained('efficientnet-b3')
    model._fc = nn.Linear(in_features=model._fc.in_features, out_features=1, bias=True)

    model._fc.weight.requires_grad = False
    model._fc.bias.requires_grad = False

    bs = 32
    size = 300

    xtra_tfms=contrast(scale=1.21)
    tfms = get_transforms(do_flip=True, flip_vert=True, max_rotate=360, max_zoom=1.3)

    old_data = (ImageList.from_df(df=old_df, path='./', cols='path')
        .split_none()
        .label_from_df(cols='diagnosis', label_cls=FloatList)
        .transform(tfms=tfms, size=size, resize_method=ResizeMethod.SQUISH, padding_mode='zeros') 
        .databunch(bs=bs, num_workers=cpu_count()) 
        .normalize(imagenet_stats)  
       )

    learn = Learner(data=old_data, 
                model=model, 
                path='../',
                model_dir='old_weights',
                metrics=[qk]).to_fp16()

    learn.fit_one_cycle(15, 0.0005)

    model._fc.weight.requires_grad = True
    model._fc.bias.requires_grad = True

    data = (ImageList.from_df(df=df, path='./', cols='path') 
        .split_by_rand_pct(0.2) 
        .label_from_df(cols='diagnosis', label_cls=FloatList)
        .transform(tfms=tfms, size=size, resize_method=ResizeMethod.SQUISH, padding_mode='zeros') 
        .databunch(bs=bs, num_workers=cpu_count()) 
        .normalize(imagenet_stats)  
       )

    learn = Learner(data, 
                model,   
                path='../',
                model_dir='weights',
                metrics=[qk]).to_fp16()

    learn.data.add_test(ImageList.from_df(test_df,
                                      os.path.join('..', 'input', 'aptos2019-blindness-detection'),
                                      folder='test_images',
                                      suffix='.png'))

    coefficients=[0.5, 1.5, 2.5, 3.5]
    opt = OptimizedRounder()
    preds,y = learn.get_preds(ds_type=DatasetType.Test)
    all_preds = list(learn.tta_only(ds_type=DatasetType.Test, activ=None, scale=1.35))
    avg_preds = torch.stack(all_preds)
    avg_preds = avg_preds.detach().cpu().numpy()
    avg_preds = np.mean(avg_preds, 0)
    preds = preds.detach().cpu().numpy()*0.4 + avg_preds*(1-0.4)
    test_pred = opt.predict(preds, coefficients)
    test_df['diagnosis'] = test_pred.astype(int)
    test_df.to_csv('submission.csv', index=False)                        

if __name__ == '__main__':
    main()