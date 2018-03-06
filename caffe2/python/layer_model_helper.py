# Copyright (c) 2016-present, Facebook, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################

## @package layer_model_helper
# Module caffe2.python.layer_model_helper
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from caffe2.python import core, model_helper, schema, scope
from caffe2.python.modeling.parameter_info import (
    ParameterInfo,
)
from caffe2.python.modeling.parameter_sharing import (
    parameter_sharing_context,
)
from caffe2.python.optimizer import get_param_device
from caffe2.python.regularizer import Regularizer
from caffe2.python.layers import layers
from caffe2.proto import caffe2_pb2
from future.utils import viewitems, viewvalues

import logging
import numpy as np
import six
import copy
logger = logging.getLogger(__name__)


class LayerModelHelper(model_helper.ModelHelper):
    """
    Model helper for building models on top of layers abstractions.

    Each layer is the abstraction that is higher level than Operator. Layer
    is responsible for ownership of it's own parameters and can easily be
    instantiated in multiple nets possible with different sets of ops.
    As an example: one can easily instantiate predict and train nets from
    the same set of layers, where predict net will have subset of the
    operators from train net.
    """

    def __init__(self, name, input_feature_schema, trainer_extra_schema,
                 keep_blobs=False):
        ''' TODO(amalevich): more documnetation on input args
        '''

        super(LayerModelHelper, self).__init__(name=name)
        self._layer_names = set()
        self._layers = []
        self._param_to_shape = {}

        # seed default
        self._seed = None
        self._sequence_seed = True

        # optimizer bookkeeping
        self.param_to_optim = {}
        self.param_to_reg = {}

        self._default_optimizer = None
        self._loss = None
        self._output_schema = None

        # breakdown map; breakdown features are categorical (like dense) but not
        # necessarily used to represent data for training
        self._breakdown_map = None

        # Connect Schema to self.net. That particular instance of schmea will be
        # use for generation of the Layers accross the network and would be used
        # for connection with Readers.
        self._input_feature_schema = schema.NewRecord(
            self.net,
            input_feature_schema
        ) if not keep_blobs else input_feature_schema.clone()
        self._trainer_extra_schema = schema.NewRecord(
            self.net,
            trainer_extra_schema
        ) if not keep_blobs else trainer_extra_schema.clone()
        self._metrics_schema = schema.Struct()

        self._init_global_constants()
        self.param_init_net = self.create_init_net('param_init_net')
        self._initialize_params = True

    def clear_output_schema(self):
        self._output_schema = None

    def set_initialize_params(self, initialize_params):
        self._initialize_params = initialize_params

    def add_metric_field(self, name, value):
        assert name not in self._metrics_schema.fields, (
            "Try to add metric field twice: {}".format(name))
        self._metrics_schema = self._metrics_schema + schema.Struct(
            (name, value)
        )

    @staticmethod
    def _get_global_constant_initializer_op(
        blob_name, array=None, dtype=None, initializer=None
    ):
        # to add a global constant to model, one first need to get the
        # initializer
        if array is not None:
            assert initializer is None,\
                "Only one from array and initializer should be specified"
            if dtype is None:
                array = np.array(array)
            else:
                array = np.array(array, dtype=dtype)

            # TODO: make GivenTensor generic
            op_name = None
            if array.dtype == np.int32:
                op_name = 'GivenTensorIntFill'
            elif array.dtype == np.int64:
                op_name = 'GivenTensorInt64Fill'
            elif array.dtype == np.str:
                op_name = 'GivenTensorStringFill'
            elif array.dtype == np.bool:
                op_name = 'GivenTensorBoolFill'
            else:
                op_name = 'GivenTensorFill'

            def initializer(blob_name):
                return core.CreateOperator(
                    op_name, [],
                    blob_name,
                    shape=array.shape,
                    values=array.flatten().tolist()
                )
        else:
            assert initializer is not None
        initializer_op = initializer(blob_name)
        return initializer_op

    def add_global_constant(
        self, name, array=None, dtype=None, initializer=None
    ):
        assert isinstance(name, six.string_types), (
            'name should be a string as we are using it as map key')
        # This is global namescope for constants. They will be created in all
        # init_nets and there should be very few of them.
        assert name not in self.global_constants, \
            "%s already added in global_constants" % name
        blob_name = self.net.NextBlob(name)
        self.global_constants[name] = blob_name
        initializer_op = LayerModelHelper._get_global_constant_initializer_op(
            blob_name, array, dtype, initializer
        )
        assert blob_name not in self.global_constant_initializers, \
            "there is already a initializer op associated with blob %s" % \
            blob_name
        self.global_constant_initializers[blob_name] = initializer_op
        return blob_name

    def maybe_add_global_constant(self, name, *args, **kwargs):
        # To ad hoc add new global constants without duplication
        # if the name was already registered in global_constants, it will not be
        # added even if the intended value is different from its original value

        def op_equal(operator1, operator2):
            o1 = copy.deepcopy(operator1)
            o2 = copy.deepcopy(operator2)
            # debug_info is supposed to be different, and we don't need to
            # compare debug_info
            if hasattr(o1, 'debug_info'):
                o1.debug_info = ''
            if hasattr(o2, 'debug_info'):
                o2.debug_info = ''
            return o1 == o2

        if name in self.global_constants:
            blob_name = self.global_constants[name]
            initializer_op = \
                LayerModelHelper._get_global_constant_initializer_op(
                    blob_name, *args, **kwargs
                )
            # check if the original initializer is the same as the one intended
            # now
            assert op_equal(initializer_op,
                            self.global_constant_initializers[blob_name]), \
                "conflict initializers for global constant %s, " \
                "previous %s, now %s" % (
                    blob_name, str(initializer_op),
                    str(self.global_constant_initializers[blob_name]))
            return blob_name
        return self.add_global_constant(name, *args, **kwargs)

    def _init_global_constants(self):
        self.global_constants = {}
        self.global_constant_initializers = {}
        self.add_global_constant('ONE', 1.0)
        self.add_global_constant('ZERO', 0.0)
        self.add_global_constant('ZERO_RANGE', [0, 0], dtype='int32')

    def _add_global_constants(self, init_net):
        for initializer_op in viewvalues(self.global_constant_initializers):
            init_net._net.op.extend([initializer_op])

    def create_init_net(self, name):
        init_net = core.Net(name)
        self._add_global_constants(init_net)
        return init_net

    def _validate_param_shape(self, param_name, shape):
        if param_name not in self._param_to_shape:
            return

        ref_shape = self._param_to_shape[param_name]

        if shape != ref_shape:
            raise ValueError(
                "Got inconsistent shapes between shared parameters "
                "when trying to map a blob in scope {0} to {1}. ref_shape : "
                " {2}, shape : {3}".format(
                    scope.CurrentNameScope(), param_name, ref_shape, shape)
            )

    def create_param(self, param_name, shape, initializer, optimizer=None,
                     ps_param=None, regularizer=None):
        if isinstance(param_name, core.BlobReference):
            param_name = str(param_name)
        elif isinstance(param_name, six.string_types):
            # Parameter name will be equal to current Namescope that got
            # resolved with the respect of parameter sharing of the scopes.
            param_name = parameter_sharing_context.get_parameter_name(
                param_name)
        else:
            raise "Unsupported type for param_name"

        param_blob = core.BlobReference(param_name)

        if len(initializer) == 1:
            init_op_args = {}
        else:
            assert len(initializer) == 2
            init_op_args = copy.deepcopy(initializer[1])
        if shape is not None:
            assert 'shape' not in init_op_args
            init_op_args.update({'shape': shape})

        initializer_op = None
        if self._initialize_params:
            initializer_op = core.CreateOperator(
                initializer[0],
                [],
                param_blob,
                **init_op_args
            )

        param = layers.LayerParameter(
            parameter=param_blob,
            initializer=initializer_op,
            optimizer=optimizer,
            ps_param=ps_param,
            regularizer=regularizer
        )

        self._validate_param_shape(param_name, shape)

        self._param_to_shape[param_name] = shape

        return param

    def next_layer_name(self, prefix):
        base_name = core.ScopedName(prefix)
        name = base_name
        index = 0
        while name in self._layer_names:
            name = base_name + '_auto_' + str(index)
            index += 1

        self._layer_names.add(name)
        return name

    def add_layer(self, layer):
        self._layers.append(layer)
        for param in layer.get_parameters():
            assert isinstance(param.parameter, core.BlobReference)

            self.param_to_optim[str(param.parameter)] = \
                param.optimizer or self.default_optimizer

            self.params.append(param.parameter)
            if isinstance(param, layers.LayerParameter):
                self.param_to_reg[param.parameter] = param.regularizer
            elif isinstance(param, ParameterInfo):
                # TODO:
                # Currently, LSTM and RNNcells, which use ModelHelper instead of
                # LayerModelHelper as super class, are called in pooling_methods
                # In ModelHelper, regularization is not supported in create_param
                # We will unify the way of create_param of ModelHelper and
                # LayerModelHelper in the future.
                logger.info('regularization is unsupported for ParameterInfo object')
            else:
                raise ValueError(
                    'unknown object type besides ParameterInfo and LayerParameter: {}'
                    .format(param)
                )

        # The primary value of adding everything to self.net - generation of the
        # operators right away, i.e. if error happens it'll be detected
        # immediately. Other than this - create_x_net should be called.
        layer.add_operators(self.net, self.param_init_net)
        return layer.output_schema

    def get_parameter_blobs(self):
        param_blobs = []
        for layer in self._layers:
            for param in layer.get_parameters():
                param_blobs.append(param.parameter)

        return param_blobs

    @property
    def seed(self):
        return self._seed

    def store_seed(self, seed, sequence_seed=True):
        # Store seed config that will be applied to each op in the net.
        self._seed = seed
        # If sequence_seed is True, the i-th op has rand_seed=`seed + i`
        self._sequence_seed = sequence_seed

    def apply_seed(self, net):
        if self._seed:
            net.set_rand_seed(self._seed, self._sequence_seed)

    @property
    def default_optimizer(self):
        return self._default_optimizer

    @default_optimizer.setter
    def default_optimizer(self, optimizer):
        self._default_optimizer = optimizer

    @property
    def input_feature_schema(self):
        return self._input_feature_schema

    @property
    def trainer_extra_schema(self):
        return self._trainer_extra_schema

    @property
    def metrics_schema(self):
        """
        Returns the schema that represents model output that should be used for
        metric reporting.

        During the training/evaluation this schema will be appended to the
        schema that represents model output.
        """
        return self._metrics_schema

    @property
    def output_schema(self):
        assert self._output_schema is not None
        return self._output_schema

    @output_schema.setter
    def output_schema(self, schema):
        assert self._output_schema is None
        self._output_schema = schema

    @property
    def loss(self):
        assert self._loss is not None
        return self._loss

    @loss.setter
    def loss(self, loss):
        assert self._loss is None
        self._loss = loss

    def has_loss(self):
        return self._loss is not None

    def add_loss(self, loss, name='unnamed'):
        assert loss is not None, "Added loss should not be None"
        assert isinstance(loss, schema.Scalar) or isinstance(
            loss, schema.Struct
        ), "Added loss should be a scalar or a struct"
        if self._loss is None:
            self._loss = schema.Struct((name, loss))
        else:
            prefix_base = name + '_auto_'
            index = 0
            prefix = name
            while prefix in self._loss:
                prefix = prefix_base + str(index)
                index += 1
            loss_struct = schema.Struct((prefix, loss))
            self._loss = self._loss + loss_struct

    def add_trainer_extra_schema(self, trainer_extra_schema):
        trainer_extra_record = schema.NewRecord(self.net, trainer_extra_schema)
        self._trainer_extra_schema += trainer_extra_record

    def __getattr__(self, layer):
        if layer.startswith('__'):
            raise AttributeError(layer)

        # TODO(amalevich): Add add support for ifbpy inline documentation
        if layers.layer_exists(layer):
            def wrapper(*args, **kwargs):
                new_layer = layers.create_layer(layer, self, *args, **kwargs)
                if kwargs.get("output_to_metrics", False):
                    new_layer.export_output_for_metrics()
                if kwargs.get("params_to_metrics", False):
                    new_layer.export_params_for_metrics()
                return self.add_layer(new_layer)
            return wrapper
        elif core.IsOperator(layer):
            def wrapper(*args, **kwargs):
                def apply_operator(net, in_record, out_record, **kwargs):
                    # TODO(amalevich): Switch to net.operator as soon as it gets
                    # landed
                    net.__getattr__(layer)(in_record.field_blobs(),
                                           out_record.field_blobs(),
                                           **kwargs)

                if 'name' not in kwargs:
                    kwargs['name'] = layer

                new_layer = layers.create_layer(
                    'Functional',
                    self, *args, function=apply_operator,
                    **kwargs
                )

                if kwargs.get("output_to_metrics", False):
                    new_layer.export_output_for_metrics()
                if kwargs.get("params_to_metrics", False):
                    new_layer.export_params_for_metrics()

                return self.add_layer(new_layer)
            return wrapper
        else:
            raise ValueError(
                "Trying to create non-registered layer: {}".format(layer))

    @property
    def layers(self):
        return self._layers

    def apply_regularizers_on_loss(
        self,
        train_net,
        train_init_net,
        blob_to_device=None,
    ):
        for param, regularizer in viewitems(self.param_to_reg):
            if regularizer is None or regularizer.apply_after_optimizer:
                continue
            assert isinstance(regularizer, Regularizer)
            added_loss_blob = regularizer(train_net, train_init_net, param)
            self.add_loss(
                schema.Scalar(blob=added_loss_blob),
                str(added_loss_blob)
            )

    def apply_regularizers_after_optimizer(
        self,
        train_net,
        train_init_net,
        grad_map,
        blob_to_device=None,
    ):
        for param, regularizer in viewitems(self.param_to_reg):
            if regularizer is None or not regularizer.apply_after_optimizer:
                continue
            assert isinstance(regularizer, Regularizer)
            regularizer(
                train_net, train_init_net, param, grad_map.get(str(param)))

    def apply_optimizers(
        self,
        train_net,
        train_init_net,
        grad_map,
        blob_to_device=None,
    ):
        CPU = core.DeviceOption(caffe2_pb2.CPU)
        # if given, blob_to_device is a map from blob to device_option
        blob_to_device = blob_to_device or {}
        for param, optimizer in viewitems(self.param_to_optim):
            assert optimizer is not None, \
                "default optimizer must have been set in add_layer"
            # note that not all params has gradient and thus we sent None if
            # gradient does not exists
            device = get_param_device(
                param,
                grad_map.get(str(param)),
                param_to_device=blob_to_device,
                default_device=CPU,
            )
            with core.DeviceScope(device):
                optimizer(
                    train_net, train_init_net, param, grad_map.get(str(param)))

    def _GetOne(self):
        return self.global_constants['ONE']

    # An optimizer which allows us to do NO optimization
    def NoOptim(self, *args, **kwargs):
        pass

    @property
    def breakdown_map(self):
        return self._breakdown_map

    @breakdown_map.setter
    def breakdown_map(self, breakdown_map):
        # TODO(xlwang): provide more rich feature information in breakdown_map;
        # and change the assertion accordingly
        assert isinstance(breakdown_map, dict)
        assert all(isinstance(k, six.string_types) for k in breakdown_map)
        assert sorted(list(breakdown_map.values())) == range(len(breakdown_map))
        self._breakdown_map = breakdown_map
