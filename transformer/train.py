"""Training code for synchronous multimodal LSTM model."""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import sys, os, shutil
import argparse
import copy

import pandas as pd
import numpy as np
from scipy.stats import pearsonr
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader

from datasets import seq_collate_dict, load_dataset
from models import MultiLSTM, MultiEDLSTM, MultiARLSTM, MultiCNNLSTM
from multiTransformer import NLPTransformer

from random import shuffle
from operator import itemgetter
import pprint

import logging
logFilename = "./train_cnn.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[
        logging.FileHandler(logFilename, 'w'),
        logging.StreamHandler()
    ])
logger = logging.getLogger()

def eval_ccc(y_true, y_pred):
    """Computes concordance correlation coefficient."""
    true_mean = np.mean(y_true)
    true_var = np.var(y_true)
    pred_mean = np.mean(y_pred)
    pred_var = np.var(y_pred)
    covar = np.cov(y_true, y_pred, bias=True)[0][1]
    ccc = 2*covar / (true_var + pred_var +  (pred_mean-true_mean) ** 2)
    return ccc

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]

def generateTrainBatch(input_data, input_target, input_length, args, batch_size=25):
    # (data, target, mask, lengths)
    input_size = len(input_data)
    index = [i for i in range(0, input_size)]
    shuffle(index)
    shuffle_chunks = [i for i in chunks(index, batch_size)]
    for chunk in shuffle_chunks:
        data_chunk = [input_data[index] for index in chunk] # <- ~batch_size, x, y, z
        target_chunk = [input_target[index] for index in chunk] # <- ~batch_size, x
        length_chunk = [input_length[index] for index in chunk] # <- ~batch_size
        # print(length_chunk)

        max_length = max(length_chunk)

        combined_data = list(zip(data_chunk, length_chunk))
        combined_data.sort(key=itemgetter(1),reverse=True)
        combined_rating = list(zip(target_chunk, length_chunk))
        combined_rating.sort(key=itemgetter(1),reverse=True)
        data_sort = []
        target_sort = []
        length_sort = []
        for pair in combined_data:
            data_sort.append(pair[0])
            length_sort.append(pair[1])

        for pair in combined_rating:
            target_sort.append(pair[0])

        data_sort = torch.tensor(data_sort, dtype=torch.float)
        target_sort = torch.tensor(target_sort, dtype=torch.float)
        old_length_sort = copy.deepcopy(length_sort)
        length_sort = torch.tensor(length_sort)
        data_sort = data_sort[:,:max_length,:,:]
        target_sort = target_sort[:,:max_length]

        lstm_masks = torch.zeros(data_sort.size()[0], data_sort.size()[1], 1, dtype=torch.float)
        for i in range(lstm_masks.size()[0]):
            lstm_masks[i,:old_length_sort[i]] = 1
        # print(lstm_masks.size())
        # print(data_sort.size())
        # print(target_sort.size())
        # length_sort = torch.tensor(length_sort, dtype=torch.float)
        yield (data_sort, torch.unsqueeze(target_sort, dim=2), lstm_masks, old_length_sort)

def train(input_data, input_target, lengths, model, criterion, optimizer, epoch, args):
    model.train()
    data_num = 0
    loss = 0.0
    batch_num = 0
    # batch our data
    for (data, target, mask, lengths) in generateTrainBatch(input_data,
                                                            input_target,
                                                            lengths,
                                                            args):
        # send to device
        mask = mask.to(args.device)
        data = data.to(args.device)
        target = target.to(args.device)
        # lengths = lengths.to(args.device)
        # Run forward pass.
        output = model(data, lengths, mask)
        # Compute loss and gradients
        batch_loss = criterion(output, target)
        # Accumulate total loss for epoch
        loss += batch_loss
        # Average over number of non-padding datapoints before stepping
        batch_loss /= sum(lengths)
        batch_loss.backward()
        # Step, then zero gradients
        optimizer.step()
        optimizer.zero_grad()
        # Keep track of total number of time-points
        data_num += sum(lengths)
        logger.info('Batch: {:5d}\tLoss: {:2.5f}'.\
              format(batch_num, loss/data_num))
        batch_num += 1
    # Average losses and print
    loss /= data_num
    logger.info('---')
    logger.info('Epoch: {}\tLoss: {:2.5f}'.format(epoch, loss))
    return loss

def evaluate(input_data, input_target, lengths, model, criterion, args, fig_path=None):
    model.eval()
    predictions = []
    data_num = 0
    loss, corr, ccc = 0.0, [], []
    count = 0

    local_best_output = []
    local_best_target = []
    local_best_index = 0
    index = 0
    local_best_ccc = -1
    for (data, target, mask, lengths) in generateTrainBatch(input_data,
                                                            input_target,
                                                            lengths,
                                                            args,
                                                            batch_size=1):
        # send to device
        mask = mask.to(args.device)
        data = data.to(args.device)
        target = target.to(args.device)
        # Run forward pass
        output = model(data, lengths, mask)
        # Compute loss
        loss += criterion(output, target)
        # Keep track of total number of time-points
        data_num += sum(lengths)
        # Compute correlation and CCC of predictions against ratings
        output = torch.squeeze(torch.squeeze(output, dim=2), dim=0).cpu().numpy()
        target = torch.squeeze(torch.squeeze(target, dim=2), dim=0).cpu().numpy()
        if count == 0:
            # print(output)
            # print(target)
            count += 1
        curr_ccc = eval_ccc(output, target)
        corr.append(pearsonr(output, target)[0])
        ccc.append(curr_ccc)
        index += 1
        if curr_ccc > local_best_ccc:
            local_best_output = output
            local_best_target = target
            local_best_index = index
            local_best_ccc = curr_ccc
    # Average losses and print
    loss /= data_num
    # Average statistics and print
    stats = {'corr': np.mean(corr), 'corr_std': np.std(corr),
             'ccc': np.mean(ccc), 'ccc_std': np.std(ccc), 'max_ccc': local_best_ccc}
    logger.info('Evaluation\tLoss: {:2.5f}\tCorr: {:0.3f}\tCCC: {:0.9f}'.\
          format(loss, stats['corr'], stats['ccc']))
    return predictions, loss, stats, (local_best_output, local_best_target, local_best_index)

def plot_predictions(dataset, predictions, metric, args, fig_path=None):
    """Plots predictions against ratings for representative fits."""
    # Select top 4 and bottom 4
    sel_idx = np.concatenate((np.argsort(metric)[-4:][::-1],
                              np.argsort(metric)[:4]))
    sel_metric = [metric[i] for i in sel_idx]
    sel_true = [dataset.orig['ratings'][i] for i in sel_idx]
    sel_pred = [predictions[i] for i in sel_idx]
    for i, (true, pred, m) in enumerate(zip(sel_true, sel_pred, sel_metric)):
        j, i = (i // 4), (i % 4)
        args.axes[i,j].cla()
        args.axes[i,j].plot(true, 'b-')
        args.axes[i,j].plot(pred, 'c-')
        args.axes[i,j].set_xlim(0, len(true))
        args.axes[i,j].set_ylim(-1, 1)
        args.axes[i,j].set_title("Fit = {:0.3f}".format(m))
    plt.tight_layout()
    plt.draw()
    if fig_path is not None:
        plt.savefig(fig_path)
    plt.pause(1.0 if args.test else 0.001)

def save_predictions(dataset, predictions, path):
    for p, seq_id in zip(predictions, dataset.seq_ids):
        df = pd.DataFrame(p, columns=['rating'])
        fname = "target_{}_{}_normal.csv".format(*seq_id)
        df.to_csv(os.path.join(path, fname), index=False)

def save_params(args, model, train_stats, test_stats):
    fname = 'param_hist.tsv'
    df = pd.DataFrame([vars(args)], columns=vars(args).keys())
    df = df[['modalities', 'batch_size', 'split', 'epochs', 'lr',
             'sup_ratio', 'base_rate']]
    for k in ['ccc_std', 'ccc']:
        v = train_stats.get(k, float('nan'))
        df.insert(0, 'train_' + k, v)
    for k in ['ccc_std', 'ccc']:
        v = test_stats.get(k, float('nan'))
        df.insert(0, 'test_' + k, v)
    df.insert(0, 'model', [model.__class__.__name__])
    df['embed_dim'] = model.embed_dim
    df['h_dim'] = model.h_dim
    df['attn_len'] = model.attn_len
    if type(model) is MultiARLSTM:
        df['ar_order'] = [model.ar_order]
    else:
        df['ar_order'] = [float('nan')]
    df.set_index('model')
    df.to_csv(fname, mode='a', header=(not os.path.exists(fname)), sep='\t')
        
def save_checkpoint(modalities, model, path):
    checkpoint = {'modalities': modalities, 'model': model.state_dict()}
    torch.save(checkpoint, path)

def load_checkpoint(path, device):
    checkpoint = torch.load(path, map_location=device)
    return checkpoint

def load_data(modalities, data_dir):
    print("Loading data...")
    train_data = load_dataset(modalities, data_dir, 'Train',
                              base_rate=args.base_rate,
                              truncate=True, item_as_dict=True)
    test_data = load_dataset(modalities, data_dir, 'Valid',
                             base_rate=args.base_rate,
                             truncate=True, item_as_dict=True)
    print("Done.")
    return train_data, test_data

def videoInputHelper(input_data, window_size, channel, old_version=False):
    # channel features
    vectors_raw = input_data[channel]
    ts = input_data[channel+"_timer"]
    # remove nan values
    vectors = []
    for vec in vectors_raw:
        inner_vec = []
        for v in vec:
            if np.isnan(v):
                inner_vec.append(0)
            else:
                inner_vec.append(v)
        vectors.append(inner_vec)

    video_vs = []
    if not old_version:
        count_v = 0
        current_time = 0.0
        window_vs = []
        while count_v < len(vectors):
            t = ts[count_v]
            if t <= current_time + window_size:
                window_vs.append(vectors[count_v])
                count_v += 1
            else:
                video_vs.append(window_vs)
                window_vs = []
                current_time += window_size
    else:
        count_v = 0
        current_time = 0.0
        window_vs = []
        for vec in vectors:
            offset = ts[count_v]
            if offset <= current_time + window_size:
                window_vs.append(vec)
            else:
                video_vs.append(window_vs)
                window_vs = [vec]
                current_time += window_size
            count_v += 1

    return video_vs

def ratingInputHelper(input_data, window_size):
    # ratings
    ratings = input_data['ratings']
    video_rs = []
    window_size_c = window_size/0.5
    rating_sum = 0.0
    for i in range(0, len(ratings)):
        rating_sum += ratings[i][0]
        if i != 0 and i%window_size_c == 0:
            video_rs.append((rating_sum*1.0/window_size_c))
            rating_sum = 0.0
    return video_rs

'''
Construct inputs for different channels: emotient, linguistic, ratings, etc..
'''
def constructInput(input_data, window_size=5, channels=['linguistic']):
    ret_input_features = {}
    ret_ratings = []
    for data in input_data:
        # channel features
        minL = 99999999
        for channel in channels:
            video_vs = videoInputHelper(data, window_size, channel)
            if channel not in ret_input_features.keys():
                ret_input_features[channel] = []
            ret_input_features[channel].append(video_vs)
            if len(video_vs) < minL:
                minL = len(video_vs)
        video_rs = ratingInputHelper(data, window_size)
        if len(video_rs) < minL:
            minL = len(video_rs)
        # concate
        for channel in channels:
             ret_input_features[channel][-1] = ret_input_features[channel][-1][:minL]
        ret_ratings.append(video_rs[:minL])
    return ret_input_features, ret_ratings

def padInputHelper(input_data, dim, old_version=False):
    output = []
    max_num_vec_in_window = 0
    max_num_windows = 0
    seq_lens = []
    for data in input_data:
        if max_num_windows < len(data):
            max_num_windows = len(data)
        seq_lens.append(len(data))
        if max_num_vec_in_window < max([len(w) for w in data]):
            max_num_vec_in_window = max([len(w) for w in data])

    padVec = [0.0]*dim
    for vid in input_data:
        vidNewTmp = []
        for wind in vid:
            if not old_version:
                # window might not contain any vector due to null during this window
                if len(wind) != 0:
                    windNew = [padVec] * max_num_vec_in_window
                    # pad with last frame features in this window
                    windNew[:len(wind)] = wind
                    vidNewTmp.append(windNew)
                    # update the pad vec to be the last avaliable vector
                else:
                    windNew = [padVec] * max_num_vec_in_window
                    vidNewTmp.append(windNew)
            else:
                windNew = [padVec] * max_num_vec_in_window
                windNew[:len(wind)] = wind
                vidNewTmp.append(windNew)
        vidNew = [[padVec] * max_num_vec_in_window]*max_num_windows
        vidNew[:len(vidNewTmp)] = vidNewTmp
        output.append(vidNew)
    return output, seq_lens

'''
pad every sequence to max length, also we will be padding windows as well
'''
def padInput(input_data, channels, dimensions):
    # input_features <- list of dict: {channel_1: [117*features],...}
    ret = {}
    seq_lens = []
    for channel in channels:
        pad_channel, seq_lens = padInputHelper(input_data[channel], dimensions[channel])
        ret[channel] = pad_channel
    return ret, seq_lens

'''
pad targets
'''
def padRating(input_data, max_len):
    output = []
    # pad ratings
    for rating in input_data:
        ratingNew = [0]*max_len
        ratingNew[:len(rating)] = rating
        output.append(ratingNew)
    return output

def main(args):
    # Fix random seed
    torch.manual_seed(1)
    torch.cuda.manual_seed(1)
    np.random.seed(1)

    # Convert device string to torch.device
    args.device = (torch.device(args.device) if torch.cuda.is_available()
                   else torch.device('cpu'))
    args.modalities = ['emotient']
    mod_dimension = {'linguistic' : 300, 'emotient' : 20, 'acoustic' : 988}
    # Load data for specified modalities
    train_data, test_data = load_data(args.modalities, args.data_dir)
    # setting
    window_size = 2
    # training data
    input_features_train, ratings_train = constructInput(train_data, channels=args.modalities, window_size=window_size)
    input_padded_train, seq_lens_train = padInput(input_features_train, args.modalities, mod_dimension)
    ratings_padded_train = padRating(ratings_train, max(seq_lens_train))
    # testing data
    input_features_test, ratings_test = constructInput(test_data, channels=args.modalities, window_size=window_size)
    input_padded_test, seq_lens_test = padInput(input_features_test, args.modalities, mod_dimension)
    ratings_padded_test = padRating(ratings_test, max(seq_lens_test))
    
    # TODO: remove this
    input_train = input_padded_train[args.modalities[0]]
    input_test = input_padded_test[args.modalities[0]]
    # construct model
    model_modalities = args.modalities
    model = MultiCNNLSTM(mods=args.modalities, dims=mod_dimension, device=args.device,
                         window_embed_size=64)

    criterion = nn.MSELoss(reduction='sum')
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-4)

    # Train and save best model
    best_ccc = -1
    single_best_ccc = -1
    for epoch in range(1, args.epochs+1):
        print('---')
        train(input_train, ratings_padded_train, seq_lens_train,
              model, criterion, optimizer, epoch, args)
        if epoch % args.eval_freq == 0:
            with torch.no_grad():
                pred, loss, stats, (local_best_output, local_best_target, local_best_index) =\
                    evaluate(input_test, ratings_padded_test, seq_lens_test,
                             model, criterion, args)
            if stats['ccc'] > best_ccc:
                best_ccc = stats['ccc']
                path = os.path.join("./lstm_save", 'multiTransformer_best.pth') 
                save_checkpoint(args.modalities, model, path)
            if stats['max_ccc'] > single_best_ccc:
                single_best_ccc = stats['max_ccc']
                logger.info('===single_max_predict===')
                logger.info(local_best_output)
                logger.info(local_best_target)
                logger.info(local_best_index)
                logger.info('===end single_max_predict===')
            logger.info('CCC_STATS\tSINGLE_BEST: {:0.9f}\tBEST: {:0.9f}'.\
            format(single_best_ccc, best_ccc))

    return best_ccc

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--modalities', type=str, default=None, nargs='+',
                        help='input modalities (default: all')
    parser.add_argument('--batch_size', type=int, default=10, metavar='N',
                        help='input batch size for training (default: 10)')
    parser.add_argument('--split', type=int, default=1, metavar='N',
                        help='sections to split each video into (default: 1)')
    parser.add_argument('--epochs', type=int, default=3000, metavar='N',
                        help='number of epochs to train (default: 1000)')
    parser.add_argument('--lr', type=float, default=1e-4, metavar='LR',
                        help='learning rate (default: 1e-6)')
    parser.add_argument('--sup_ratio', type=float, default=0.5, metavar='F',
                        help='teacher-forcing ratio (default: 0.5)')
    parser.add_argument('--base_rate', type=float, default=2.0, metavar='N',
                        help='sampling rate to resample to (default: 2.0)')
    parser.add_argument('--log_freq', type=int, default=5, metavar='N',
                        help='print loss N times every epoch (default: 5)')
    parser.add_argument('--eval_freq', type=int, default=1, metavar='N',
                        help='evaluate every N epochs (default: 1)')
    parser.add_argument('--save_freq', type=int, default=10, metavar='N',
                        help='save every N epochs (default: 10)')
    parser.add_argument('--device', type=str, default='cuda:0',
                        help='device to use (default: cuda:0 if available)')
    parser.add_argument('--visualize', action='store_true', default=False,
                        help='flag to visualize predictions (default: false)')
    parser.add_argument('--normalize', action='store_true', default=False,
                        help='whether to normalize inputs (default: false)')
    parser.add_argument('--test', action='store_true', default=False,
                        help='evaluate without training (default: false)')
    parser.add_argument('--load', type=str, default=None,
                        help='path to trained model (either resume or test)')
    parser.add_argument('--data_dir', type=str, default="../data",
                        help='path to data base directory')
    parser.add_argument('--save_dir', type=str, default="./lstm_save",
                        help='path to save models and predictions')
    args = parser.parse_args()
    main(args)