# coding: utf-8
# Author: Zhongyang Zhang
# Email : mirakuruyoo@gmail.com

import json
import os
import shutil
import threading

import numpy as np
import torch
import torch.nn as nn
from scipy.stats import mode
from tensorboardX import SummaryWriter
from tqdm import tqdm

lock = threading.Lock()


class MyThread(threading.Thread):
    """
        Multi-thread support class. Used for multi-thread model
        file saving.
    """

    def __init__(self, opt, net, epoch, bs_old, loss):
        threading.Thread.__init__(self)
        self.opt = opt
        self.net = net
        self.epoch = epoch
        self.bs_old = bs_old
        self.loss = loss

    def run(self):
        lock.acquire()
        try:
            if self.opt.SAVE_TEMP_MODEL:
                self.net.save(self.epoch, self.loss, "temp_model.dat")
            if self.opt.SAVE_BEST_MODEL and self.loss < self.bs_old:
                self.net.best_loss = self.loss
                net_save_prefix = self.opt.NET_SAVE_PATH + self.opt.MODEL + '_' + self.opt.PROCESS_ID + '/'
                temp_model_name = net_save_prefix + "temp_model.dat"
                best_model_name = net_save_prefix + "best_model.dat"
                shutil.copy(temp_model_name, best_model_name)
        finally:
            lock.release()


class BasicModule(nn.Module):
    """
        Basic pytorch module class. A wrapped basic model class for pytorch models.
        You can Inherit it to make your model easier to use. It contains methods
        such as load, save, multi-thread save, parallel distribution, train, validate,
        predict and so on.
    """

    def __init__(self, opt=None, device=None):
        super(BasicModule, self).__init__()
        self.model_name = self.__class__.__name__
        self.opt = opt
        self.best_loss = 1e8
        self.pre_epoch = 0
        self.threads = []
        if device:
            self.device = device
        else:
            self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.writer = SummaryWriter(opt.SUMMARY_PATH)

    def load(self, model_type: str = "temp_model.dat", map_location=None) -> None:
        """
            Load the existing model.
            :param model_type: temp model or best model.
            :param map_location: your working environment.
                For loading the model file dumped from gpu or cpu.
            :return: None.
        """
        print('==> Now using ' + self.opt.MODEL + '_' + self.opt.PROCESS_ID)
        print('==> Loading model ...')
        if not map_location:
            map_location = self.device.type
        net_save_prefix = self.opt.NET_SAVE_PATH + self.opt.MODEL + '_' + self.opt.PROCESS_ID + '/'
        temp_model_name = net_save_prefix + model_type
        if not os.path.exists(net_save_prefix):
            os.mkdir(net_save_prefix)
        if os.path.exists(temp_model_name):
            checkpoint = torch.load(temp_model_name, map_location=map_location)
            self.pre_epoch = checkpoint['epoch']
            self.best_loss = checkpoint['best_loss']
            self.load_state_dict(checkpoint['state_dict'])
            print("==> Load existing model: %s" % temp_model_name)
        else:
            print("==> The model you want to load (%s) doesn't exist!" % temp_model_name)

    def save(self, epoch, loss, name=None):
        """
        Save the current model.
        :param epoch:The current epoch (sum up). This will be together saved to file,
            aimed to keep tensorboard curve a continuous line when you train the net
            several times.
        :param loss:Current loss.
        :param name:The name of your saving file.
        :return:None
        """
        if loss < self.best_loss:
            self.best_loss = loss
        if self.opt is None:
            prefix = "./source/trained_net/" + self.model_name + "/"
        else:
            prefix = self.opt.NET_SAVE_PATH + self.opt.MODEL + '_' + \
                     self.opt.PROCESS_ID + '/'
            if not os.path.exists(prefix): os.mkdir(prefix)

        if name is None:
            name = "temp_model.dat"

        path = prefix + name
        try:
            state_dict = self.module.state_dict()
        except:
            state_dict = self.state_dict()
        torch.save({
            'epoch': epoch + 1,
            'state_dict': state_dict,
            'best_loss': self.best_loss
        }, path)

    def mt_save(self, epoch, loss):
        """
        Save the model with a new thread. You can use this method in stead of self.save to
        save your model while not interrupting the training process, since saving big file
        is a time-consuming task.
        Also, this method will automatically record your best model and make a copy of it.
        :param epoch: Current loss.
        :param loss:
        :return: None
        """
        if self.opt.SAVE_BEST_MODEL and loss < self.best_loss:
            print("==> Your best model is renewed")
        if len(self.threads) > 0:
            self.threads[-1].join()
        self.threads.append(MyThread(self.opt, self, epoch, self.best_loss, loss))
        self.threads[-1].start()
        if self.opt.SAVE_BEST_MODEL and loss < self.best_loss:
            print("==> Your best model is renewed")
            self.best_loss = loss

    def _get_optimizer(self):
        """
        Get your optimizer by parsing your opts.
        :return:Optimizer.
        """
        if self.opt.OPTIMIZER == "Adam":
            optimizer = torch.optim.Adam(self.parameters(), lr=self.opt.LEARNING_RATE, )
        else:
            raise KeyError("==> The optimizer defined in your config file is not supported!")
        return optimizer

    def reset_module(self):
        self.model_name = self.module.model_name
        self.opt = self.module.opt
        self.best_loss = self.module.best_loss
        self.pre_epoch = self.module.pre_epoch
        self.threads = self.module.threads
        self.device = self.module.device
        self.fit = self.module.fit
        self.predict = self.module.predict
        self.validata = self.module.validate

    def to_multi(self):
        """
        If you have multiple GPUs and you want to use them at the same time, you should
        call this method before training to send your model and data to multiple GPUs.
        :return: None
        """
        if torch.cuda.is_available():
            print("==> Using", torch.cuda.device_count(), "GPUs.")
            if torch.cuda.device_count() > 1:
                self = torch.nn.DataParallel(self)
                self.module.reset_module()
                print("==> Using data parallelism.")
        else:
            print("==> Using CPU now.")
        self.to(self.device)
        print(self)

    def validate(self, eval_loader):
        """
        Validate your model.
        :param eval_loader: A DataLoader class instance, which includes your validation data.
        :return: eval loss and eval accuracy.
        """
        self.eval()
        eval_loss = 0
        eval_acc = 0
        for i, data in tqdm(enumerate(eval_loader), desc="Evaluating", total=len(eval_loader), leave=False, unit='b'):
            inputs, labels, *_ = data
            inputs, labels = inputs.to(self.device), labels.to(self.device)

            # Compute the outputs and judge correct
            outputs = self(inputs)
            loss = self.opt.CRITERION(outputs, labels)
            eval_loss += loss.item()

            predicts = outputs.sort(descending=True)[1][:, :self.opt.TOP_NUM]
            for predict, label in zip(predicts.tolist(), labels.cpu().tolist()):
                if label in predict:
                    eval_acc += 1
        return eval_loss / self.opt.NUM_EVAL, eval_acc / self.opt.NUM_EVAL

    def predict(self, eval_loader):
        """
        Make prediction based on your trained model. Please make sure you have trained
        your model or load the previous model from file.
        :param test_loader: A DataLoader class instance, which includes your test data.
        :return: Prediction made.
        """
        recorder = []
        print("==> Start predicting...")
        self.eval()
        for i, data in tqdm(enumerate(eval_loader), desc="Evaluating", total=len(eval_loader), leave=False, unit='b'):
            inputs, *_ = data
            inputs = inputs.to(self.device)
            outputs = self(inputs)
            predicts = outputs.sort(descending=True)[1][:, :self.opt.TOP_NUM]
            recorder.extend(np.array(outputs.sort(descending=True)[1]))
            json.dump(np.concatenate(recorder, 0), open("./test.json", "w+"))
        return predicts

    def vote_eval(self, eval_loader):
        print("==> Start vote predicting...")
        self.eval()
        eval_loss = 0
        eval_acc = 0
        for i, data in tqdm(enumerate(eval_loader), desc="Evaluating", total=len(eval_loader), leave=False, unit='b'):
            inputs, labels, *_ = data
            inputs, labels = inputs.to(self.device), labels.to(self.device)

            # Compute the outputs and judge correct
            outputs = self(inputs)
            loss = self.opt.CRITERION(outputs, labels)
            eval_loss += loss.item()

            predicts = outputs.sort(descending=True)[1][:, 0].detach().numpy()
            pred_vals = outputs.sort(descending=True)[0][:, 0].detach().numpy()
            valid_voters = np.where(pred_vals >= self.opt.THREADHOLD)
            valid_votes = predicts[valid_voters]
            res = mode(valid_votes)[0][0]
            print(res, labels.detach().tolist()[0], pred_vals, predicts)

            if labels.detach().tolist()[0] == res:
                eval_acc += 1

        return eval_loss / self.opt.NUM_EVAL, eval_acc / self.opt.NUM_EVAL

    def fit(self, train_loader, eval_loader):
        """
        Training process. You can use this function to train your model. All configurations
        are defined and can be modified in config.py.
        :param train_loader: A DataLoader class instance, which includes your train data.
        :param eval_loader: A DataLoader class instance, which includes your test data.
        :return: None.
        """
        print("==> Start training...")
        optimizer = self._get_optimizer()
        for epoch in range(self.opt.NUM_EPOCHS):
            train_loss = 0
            train_acc = 0

            # Start training
            self.train()
            print('==> Preparing Data ...')
            for i, data in tqdm(enumerate(train_loader), desc="Training", total=len(train_loader), leave=False,
                                unit='b'):
                inputs, labels, *_ = data
                inputs, labels = inputs.to(self.device), labels.to(self.device)
                optimizer.zero_grad()

                # forward + backward + optimize
                outputs = self(inputs)
                loss = self.opt.CRITERION(outputs, labels)
                predicts = outputs.sort(descending=True)[1][:, :self.opt.TOP_NUM]
                for predict, label in zip(predicts.tolist(), labels.cpu().tolist()):
                    if label in predict:
                        train_acc += 1

                # loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
                train_loss += loss.item()

            train_loss = train_loss / self.opt.NUM_TRAIN
            train_acc = train_acc / self.opt.NUM_TRAIN

            # Start testing
            eval_loss, eval_acc = self.validate(eval_loader)

            # Add summary to tensorboard
            self.writer.add_scalar("Train/loss", train_loss, epoch + self.pre_epoch)
            self.writer.add_scalar("Train/acc", train_acc, epoch + self.pre_epoch)
            self.writer.add_scalar("Eval/loss", eval_loss, epoch + self.pre_epoch)
            self.writer.add_scalar("Eval/acc", eval_acc, epoch + self.pre_epoch)

            # Output results
            print('Epoch [%d/%d], Train Loss: %.4f, Train Acc: %.4f, Eval Loss: %.4f, Eval Acc:%.4f'
                  % (self.pre_epoch + epoch + 1, self.pre_epoch + self.opt.NUM_EPOCHS + 1,
                     train_loss, train_acc, eval_loss, eval_acc))

            # Save the model
            if epoch % self.opt.SAVE_EVERY == 0:
                self.mt_save(self.pre_epoch + epoch + 1, eval_loss / self.opt.NUM_EVAL)

        print('==> Training Finished.')
