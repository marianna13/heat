import torch
import torch.distributed
from torch.nn.parallel import DistributedDataParallel as tDDP
from ..core.communication import MPICommunication
from ..core.communication import MPI
from ..core.communication import MPI_WORLD

from typing import Union, List, Tuple, Dict

import time

__all__ = ["DataParallelOptimizer", "SkipBatches"]


def print0(*args, **kwargs):
    if MPI_WORLD.rank == 0:
        print(*args, **kwargs)


def __sum_f16_cb(buffer_a, buffer_b, _):
    tens_a = torch.HalfTensor().set_(torch.HalfStorage.from_buffer(buffer_a, "native"))
    tens_b = torch.HalfTensor().set_(torch.HalfStorage.from_buffer(buffer_b, "native"))
    tens_b += tens_a
    nelem = torch.prod(torch.tensor(tens_b.shape)).item()
    new_buff = MPI.memory.fromaddress(tens_b.data_ptr(), nbytes=tens_b.element_size() * nelem)
    buffer_b[:] = new_buff


def __sum_bfloat_cb(buffer_a, buffer_b, _):
    tens_a = torch.BFloat16Tensor().set_(torch.BFloat16Storage.from_buffer(buffer_a, "native"))
    tens_b = torch.BFloat16Tensor().set_(torch.BFloat16Storage.from_buffer(buffer_b, "native"))
    tens_b += tens_a
    nelem = torch.prod(torch.tensor(tens_b.shape)).item()
    new_buff = MPI.memory.fromaddress(tens_b.data_ptr(), nbytes=tens_b.element_size() * nelem)
    buffer_b[:] = new_buff


# create new OP
mpi_sum_f16 = MPI.Op.Create(__sum_f16_cb, commute=True)
mpi_sum_bfloat = MPI.Op.Create(__sum_bfloat_cb, commute=True)


def addCounter(counter1, counter2, datatype):
    for item in counter2:
        if item in counter1:
            counter1[item] += counter2[item]
        else:
            counter1[item] = counter2[item]
    return counter1


counterSumOp = MPI.Op.Create(addCounter, commute=True)


class DataParallelOptimizer:
    """
    Uses a Torch.optim.Optimizer for data parallelism. It should be used in combination with DataParallel (DP) class.
    To optimize a DP module, DP optimizer has to be passed to DP module during its initialization.
    See :func:`..nn.DataParallel` for a basic example of usage.

    Attributes
    ----------
    torch_optimizer : torch.optim.Optimizer
        the wrapped Torch optimizer
    blocking : bool
        use blocking communications or not. will typically be overwritten by heat.nn.DataParallel
    """

    def __init__(self, torch_optimizer: torch.optim.Optimizer, blocking: bool = False):
        self.torch_optimizer = torch_optimizer
        if not isinstance(blocking, bool):
            raise TypeError(f"blocking parameter must be a boolean, currently {type(blocking)}")
        # flag indicating if communication during parameter updates is blocking.
        self.blocking_parameter_updates = blocking
        # flag indicating if optimizer should take a step during next iteration (only relevant for non-blocking)
        self.update_next = False
        # reference of optimizer's params
        self.params_ref = torch_optimizer.param_groups[0]["params"]

    def step(self) -> None:
        """
        Force torch optimizer to update model parameters. For blocking, optimizer immediately updates parameters. For
        non-blocking, optimizer will update parameters during next forward.
        """
        if self.blocking_parameter_updates:
            self.torch_optimizer.step()
        else:
            self.update_next = True

    def zero_grad(self) -> None:
        """
        Reset gradients of optimizer's params.
        """
        # reset view onto params in order to reset all gradients
        self.torch_optimizer.param_groups[0]["params"] = self.params_ref[:]
        self.torch_optimizer.zero_grad()


class SkipBatches:
    """
    Optimizer which skips batches
    """

    def __init__(
        self,
        local_optimizer: torch.optim.Optimizer,
        total_epochs: int,
        comm: MPICommunication = MPI_WORLD,
        warmup_epochs: int = 4,
        scheduler: torch.optim.lr_scheduler = None,
    ):
        self.comm = comm
        self.lcl_optimizer = local_optimizer
        # reference of optimizer's params
        self.scheduler = scheduler
        # TODO: remove apex stuff
        self.apex = False

        rank = comm.rank
        loc_gpus = torch.cuda.device_count()
        self.loc_gpus = loc_gpus
        local_rank = rank % loc_gpus
        self.local_skip = 1
        if loc_gpus > 1:
            base_loc_ranks = list(range(0, comm.size, loc_gpus))
            reduced_comms, reduced_ranks = [], []
            for i in range(loc_gpus):
                lp_ranks = [j + i for j in base_loc_ranks]
                color = 111 + i if rank in lp_ranks else 222 + i
                key = 0 + i if rank in lp_ranks else 444 + i
                reduced_comms.append(MPICommunication(MPI_WORLD.Split(color, key)))
                reduced_ranks.append(tuple(lp_ranks))
            self.reduced_comms, self.reduced_ranks = reduced_comms, reduced_ranks
            self.base_loc_ranks = base_loc_ranks

            self.device = "cuda:" + str(local_rank)
            torch.cuda.set_device(device=self.device)

        self.current_batch, self.last_batch = 0, None

        self._prev_params = []
        self.epoch = 0
        self._send_mod, self._send_mod_m1 = 0, None

        self._prev_losses_mean, self._prev_losses_std = [], []
        self.global_skip = 0
        self.local_skip = 0
        self.batches_to_wait = 0
        self.epochs_to_wait = 3

        self.warmup_epochs = warmup_epochs
        self.total_epochs = total_epochs

        # used in the sending of the params
        self._param_send_buffer_shape = None
        self.param_dict, self.shapes = None, None

        self.scaler = None
        self.amp = False

        if self.apex:
            self.apex_dict = {}

    def set_model(self, model):
        self.module = model

    def _stop_local_sync(self):
        # stop local synchronizations for tDDP
        if not isinstance(self.module, tDDP) or not self.module.require_backward_grad_sync:
            # this has no effect if the module is not locally distributed in torch
            return
        self.module.require_backward_grad_sync = False

    def _start_local_sync(self):
        # *start* local synchronizations for tDDP
        if not isinstance(self.module, tDDP) or self.module.require_backward_grad_sync:
            # this has no effect if the module is not locally distributed in torch
            return
        self.module.require_backward_grad_sync = True

    @torch.no_grad()
    def epoch_loss_logic(self, loss):
        loss_send = torch.zeros(self.comm.size)
        # loss.data -> this will get the raw number from the lass value and nothing else
        loss_send[self.comm.rank] = loss.data

        self.comm.Allreduce(MPI.IN_PLACE, loss_send, MPI.SUM)

        avg_loss = torch.mean(loss_send)
        self._prev_losses_mean.append(avg_loss)

        if self.epoch < self.warmup_epochs:
            self.global_skip = 0
            self.local_skip = 0
            self.batches_to_wait = 0
            print0("\t\t", self.global_skip, self.local_skip, self.batches_to_wait)
            return
        elif 4 == self.epoch:
            self.global_skip = 4
            self.local_skip = 1
            self.batches_to_wait = 1
            print0("\t\t", self.global_skip, self.local_skip, self.batches_to_wait)
            self._prev_losses_mean = []

        if self.epoch >= self.total_epochs - 5:
            self.global_skip = 0
            self.local_skip = 0
            self.batches_to_wait = 0
            print0("\t\t", self.global_skip, self.local_skip, self.batches_to_wait)
            return

        # epochs_to_wait = 3
        if len(self._prev_losses_mean) < self.epochs_to_wait:
            return
        means = torch.tensor(self._prev_losses_mean)
        diff = abs(means[-1] - means[-1 * self.epochs_to_wait])
        stable = True if diff <= 0.075 else False
        # TODO: add something for when the loss is *increasing*?
        if stable and self.global_skip > 1:
            # drop gs by factor of 2
            self.global_skip //= 2
            self.local_skip //= 2
            self.batches_to_wait //= 2
            self.epochs_to_wait += 1
            self._prev_losses_mean = []
            print0("dropping skips, loss stable")
            if self.global_skip > 0:
                if self.batches_to_wait == 0:
                    self.batches_to_wait = 1
                if self.local_skip == 0:
                    self.local_skip = 1
        elif self.global_skip == 1 and stable:
            self.global_skip = 8
            self.local_skip = 2
            self.batches_to_wait = 3  # 2
            self._prev_losses_mean = []
            self.epochs_to_wait = 3

        print0("\t\t", self.global_skip, self.local_skip, self.batches_to_wait)

    def add_scaler(self, scaler):
        self.scaler = scaler
        self.amp = True

    def step(self):
        # TODO: raise error is last batch is not set
        # collect the parameters from the current batch -> save + (non?)blocking send
        # test for receive from last batch,
        #   if yes: receive, update parameters with rcved stuff
        # copy and send the parameter dictionary
        if self.amp:
            self.scaler.step(self.lcl_optimizer)
            # Updates the scale for next iteration.
            self.scaler.update()
        elif self.scheduler is None:
            self.lcl_optimizer.step()
        else:
            self.scheduler.step()
        batch = self.current_batch
        next_batch = batch + 1
        gs = self.global_skip
        ls = self.local_skip

        gmod = batch % gs if gs > 0 else 0
        lmod = batch % ls if ls > 0 else 0

        batches_to_wait = self.batches_to_wait
        btw = (
            batches_to_wait
            if batches_to_wait + batch <= self.last_batch
            else self.last_batch - batch
        )
        # do full synce on global skips and on the last batch
        if batch == self.last_batch or gmod == 0:
            return self._full_global_sync(btw)

        if next_batch % gs == 0:
            self._start_local_sync()
            self.current_batch += 1
            return

        if gmod < btw:
            # do nothing on these batches
            self.current_batch += 1
            if next_batch == self.last_batch:
                self._start_local_sync()
            return
        elif gmod == btw:
            # local updates should be on before this is called!
            self._update_parameters()
            self._local_torch_param_update(self._send_mod_m1)
            if ls > 1:
                self._stop_local_sync()

        if ls == 1 and next_batch != self.last_batch:
            self.current_batch += 1
            self._start_local_sync()
            return

        if lmod == 0:
            self._stop_local_sync()
        elif next_batch % ls == 0:
            self._start_local_sync()

        if next_batch == self.last_batch:
            self._start_local_sync()

        self.current_batch += 1

    @torch.no_grad()
    def _full_global_sync(self, batches_to_wait):
        current_comm = self.reduced_comms[self._send_mod]
        current_ranks = self.reduced_ranks[self._send_mod]

        if self.comm.rank in current_ranks:
            self._global_send_update(current_comm, batches_to_wait)

        if self.batches_to_wait != 0:
            # update parameters from the last sending (if there)
            self._update_parameters()  # -> splits off irrelevant ranks
            # needs to happen on all ranks:
            self._local_torch_param_update(self._send_mod_m1)

        if self.current_batch == self.last_batch or self.batches_to_wait == 0:
            # todo: abstract last batch?
            # receive the sent data to sync params across all ranks
            if self.comm.rank in current_ranks:
                if len(self._prev_params) > 1:
                    raise ValueError(f"length of previous params > 1! {len(self._prev_params)}")
                prev_params = self._prev_params.pop(0)
                shapes = prev_params[2]
                prev_params[0].Wait()
                rcv_params = prev_params[1] / float(len(current_ranks))
                for name, param in self.module.named_parameters():
                    if param.requires_grad:
                        param[:] = (
                            rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                        )
                self._prev_params = []
            else:
                if len(self._prev_params) > 0:
                    raise ValueError(
                        f"DEBUG: OFF RANKS! len(prev_params) > 0! {len(self._prev_params)}"
                        f" batch number {self.current_batch}"
                    )
            self._local_torch_param_update(self._send_mod)

            self._send_mod_m1 = None

            if self.current_batch == self.last_batch:
                self._send_mod = 0
                self.epoch += 1
                self.current_batch = 0
            else:
                self.current_batch += 1
                self._send_mod = self._send_mod + 1 if self._send_mod <= self.loc_gpus - 2 else 0
        else:
            self.current_batch += 1
            self._send_mod_m1 = self._send_mod
            self._send_mod = self._send_mod + 1 if self._send_mod <= self.loc_gpus - 2 else 0

    @torch.no_grad()
    def _global_send_update(self, current_comm, batches_to_wait):
        # pack and send the data required for a global synchronization
        op = MPI.SUM
        cast = False
        if self.global_skip < 1:
            op = mpi_sum_bfloat
            cast = True

        if not self.apex:
            param_dict, shapes = self._create_param_dict_n_shapes()
            params = torch.zeros(
                self._param_send_buffer_shape,
                device=self.device,
                dtype=torch.bfloat16 if cast else None,
            )
            params = self.__pack_data(param_dict, params, cast)

            new_wait = current_comm.Iallreduce(MPI.IN_PLACE, params, op)  # mpi_sum_f16) #
            self._prev_params.append([new_wait, params, shapes, batches_to_wait, None])
            return new_wait
        else:
            self.apex_dict = self._create_apex_dict()
            params32 = torch.zeros(
                self.apex_dict["fp32"]["send-shp"],
                device=self.device,
                dtype=torch.bfloat16 if cast else None,
            )
            param_dict32 = self.apex_dict["fp32"]["param_dict"]
            shapes32 = self.apex_dict["fp32"]["shapes"]
            params32 = self.__pack_data(param_dict32, params32, cast)
            op = MPI.SUM if not cast else op
            new_wait32 = current_comm.Iallreduce(MPI.IN_PLACE, params32, op)
            self._prev_params.append([new_wait32, params32, shapes32, batches_to_wait, "fp32"])
            # float 16
            params16 = torch.zeros(
                self.apex_dict["fp16"]["send-shp"],
                device=self.device,
                dtype=torch.bfloat16 if cast else None,
            )
            param_dict16 = self.apex_dict["fp16"]["param_dict"]
            shapes16 = self.apex_dict["fp16"]["shapes"]
            # no benefit from casting to bfloat16 here
            params16 = self.__pack_data(param_dict16, params16, cast=False)
            new_wait16 = current_comm.Iallreduce(MPI.IN_PLACE, params16, mpi_sum_f16)
            self._prev_params.append([new_wait16, params16, shapes16, batches_to_wait, "fp32"])

    def _create_param_dict_n_shapes(self):
        """
        create the shape and param dictionary used for sending parameters around the MPI world.
        this will also define the buffer size if it was not previously defined.
        """
        if self.shapes is not None and self.param_dict is not None:
            return self.param_dict, self.shapes
        # else:
        param_dict = {}
        shapes = {}
        st = 0
        for name, param in self.module.named_parameters():
            param_dict[name] = param
            numel = param.numel()
            shapes[name] = [param.shape, slice(st, st + numel), param.dtype]
            st += numel
        if self._param_send_buffer_shape is None:
            # use the total number of elements to define the sending buffer shape (single int)
            self._param_send_buffer_shape = st
        self.param_dict = param_dict
        self.shapes = shapes
        return param_dict, shapes

    def _create_apex_dict(self):
        if not self.apex:
            return
        # todo: check that apex doesnt change which layers are cast each time...or check this each time
        # code to check each time -> self.apex_dict is None:
        # in this case there are should be two param dicts and two shape dicts
        if self.apex_dict != {} and self.apex_dict is not None:
            return self.apex_dict
        self.apex_dict = {
            "fp32": {"params_dict": {}, "shapes": {}, "send-shp": 0},
            "fp16": {"params_dict": {}, "shapes": {}, "send-shp": 0},
        }
        # apex dict will have the fp32 and fp16 keys for each thing
        st, st16, st32 = 0, 0, 0
        for name, param in self.module.named_parameters():
            tp = param.dtype
            numel = param.numel()
            if tp == torch.float32:
                k = "fp32"
                st += st32
                end = st + numel
                st32 = end
            elif tp == torch.float16:
                k = "fp16"
                st += st16
                end = st + numel
                st16 = end
            else:
                raise TypeError(
                    f"Unaccounted for dtype! must be either float32 or float16, "
                    f"current param: name: {name}, dtype: {param.dtype}"
                )
            self.apex_dict[k]["param_dict"][name] = param
            self.apex_dict[k]["shapes"][name] = [param.shape, slice(st, end), tp]
            st = 0
        self.apex_dict["fp32"]["send-shp"] = st32
        self.apex_dict["fp16"]["send-shp"] = st16
        return self.apex_dict

    @staticmethod
    @torch.jit.script
    def __pack_data(iter_dict: Dict[str, torch.Tensor], params: torch.Tensor, cast: bool):
        """ jitted loop to pack the data into params to be sent"""
        st = 0
        for name, param in iter_dict.items():
            if param.requires_grad:
                # flatten and prep the data for sending
                p = torch.flatten(param)
                if cast:
                    p = p.to(torch.bfloat16)
                params[st : st + param.numel()] = p
                st += param.numel()
        return params

    @torch.no_grad()
    def _local_torch_param_update(self, mod_hold_pr):
        # TODO: jit this?
        # send the globally updated gradients from `mod_hold_pr` to the other local processes
        if mod_hold_pr is None:
            # this is a failsafe in case this function is called when there is no need to synchronize
            return
        if torch.distributed.is_initialized():
            snds = {}
            for name, param in self.module.named_parameters():
                if param.requires_grad:
                    snds[name] = torch.distributed.broadcast(param, mod_hold_pr, async_op=True)
            for name, param in self.module.named_parameters():
                if param.requires_grad:
                    snds[name].wait()
            del snds

    @torch.no_grad()
    def _update_parameters(self):
        # wait for the global sync data and update on the selected rank, requires local torch param update after
        if self._send_mod_m1 is None:
            return
        prev_ranks = self.reduced_ranks[self._send_mod_m1]
        if self.comm.rank not in prev_ranks:
            # receive previous gradients
            return
        if len(self._prev_params) == 0:
            # if no old gradients, return without doing anything
            return
        # use self.param_dict if not apex, otherwise use self.apex_dict
        # if apex is there, then there are 2 buffers to receive
        # for apex_ittr in range(1 if not self.apex else 2):
        if self.apex:

            return
        prev_params = self._prev_params.pop(0)
        batches_between = float(prev_params[3])
        # add the weighted average to param
        shapes = prev_params[2]
        numer = batches_between * 2.0 if batches_between > 0.0 else 1.0
        denom = float(len(prev_ranks) + numer)
        factor = numer / denom
        prev_params[0].Wait()
        rcv_params = prev_params[1] / denom
        # todo: jit the parameter setting
        for name, param in self.module.named_parameters():
            if param.requires_grad:
                update = rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                # NOTE: update here is the sum of the params across the processes
                param *= factor
                param += update  # / denom

    def _apex_update_params(self, prev_ranks):
        for _ in range(2):
            # only two different dtypes in apex. if more are added, this would need to be increased
            prev_params = self._prev_params.pop(0)
            key = prev_params[4]
            batches_between = float(prev_params[3])
            # add the weighted average to param
            shapes = prev_params[2]
            numer = batches_between * 2.0 if batches_between > 0.0 else 1.0
            denom = float(len(prev_ranks) + numer)
            factor = numer / denom
            prev_params[0].Wait()
            rcv_params = prev_params[1] / denom
            # todo: jit the parameter setting
            for name, param in self.apex_dict[key]["params_dict"]:
                if param.requires_grad:
                    update = (
                        rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                    )
                    # NOTE: update here is the sum of the params across the processes
                    param *= factor
                    param += update  # / denom

    # @staticmethod
    # @torch.jit.script
    # def __set_params_after_recv(param_dict, factor):
    # todo: the slice to get the proper parameter numbers is a slice and
    #       cannot be passed into torch's jit function, same with dtype

    def zero_grad(self) -> None:
        """
        Reset gradients of optimizer's params.
        """
        # reset view onto params in order to reset all gradients
        self.lcl_optimizer.zero_grad()
