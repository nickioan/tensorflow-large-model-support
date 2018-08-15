# (C) Copyright IBM Corp. 2018. All Rights Reserved.
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
# ==============================================================================
"""LMS
"""
import tensorflow.contrib.graph_editor as ge
from tensorflow.contrib.graph_editor import util
from tensorflow.python.platform import tf_logging
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops

import time
from six.moves import queue as Queue
from lms import topos


class LMS(object):
    """LMS class for Large Model Support (LMS).

    The `LMS` object statically modifies a model by swapping its tensors
    to the host so that the model can be trained with the limited memory
    of GPUs.

    Tensors those are generated by forward operations and consumed by
    backward operations are candidates for swapping. The `LMS` object will
    automatically find these tensors.

    Swapping is done by cutting the link between two operations whose
    topological-sort distance between them is greater than a given
    `threshold`, then replacing the link by inserting `identity`
    operations on the host. In theory, this procedure does not have any
    effect on the training convergence as well as inference task.
    """
    def __init__(self, graph=None,
                 excl_scopes=set(),
                 incl_scopes=set(),
                 excl_types=set(),
                 incl_types=set(),
                 swapout_threshold=5,
                 swapin_groupby=5,
                 swapin_ahead=1,
                 debug=False,
                 debug_level=1,
                 cpu_device="/cpu:0"):
        """Create an LMS object to edit the graph for supporting large model.

        Args:
          graph: the graph we will modify for LMS. This should be the graph of
            user-defined neural network.
          excl_scopes: a set of scopes for operations whose tensors will not
            be swapped out to the host. Default `empty`.
          incl_scopes: a set of scopes for operations whose tensors will be
            swapped out to the host. Default `empty`.
          excl_types: a set of types for operations whose tensors will not be
            swapped out to the host. Default `empty`.
          incl_types: a set of types for operations whose tensors will be
            swapped out to the host. Default `empty`.
          swapout_threshold: if the topological-sort distance between the consuming
            operation and generating operation of a tensor is greater (>) than
            `swapout_threshold`, then trigger swapping the tensor. Default `0`.
          swapin_groupby: consuming operations whose distances among them are
            within `swapin_groupby` share the same swap-in operation.
          swapin_ahead: lower-bound value for LMS. A tensor will be swapped in during the
            backward phase at least `swapin_ahead` nodes before it in the graph.
            Default `1`.
          debug: debug mode for LMS. Default `False`.
          debug_level: debug level for LMS (1 or 2). Default `1`.
          cpu_device: the device we would like swap tensors to.
        """
        self._graph = graph
        self._excl_scopes = excl_scopes
        self._incl_scopes = incl_scopes
        self._excl_types = excl_types
        self._incl_types = incl_types

        self._swapout_threshold = swapout_threshold
        self._swapin_groupby = swapin_groupby
        self._swapin_ahead = swapin_ahead

        # Operations with these types will be ignored
        self._excl_types |= {'Const', 'Identity'}
        self._excl_types |= {'Placeholder'}  # input data
        self._excl_types |= {'VariableV2'}  # learnable parameters

        self._excl_ops = set()
        self._incl_ops = set()
        self._topo_sort = None
        self._cpu_device = cpu_device
        self._debug = debug
        self._debug_level = debug_level

        # keep log of tensors on host
        self._swapout_tensors = 0
        self._swapin_tensors = 0
        # added ops
        self._added_ops = set()

        # store a dictionary of visited ops to avoid multiple visits
        self._ops_dict = {}

    def _filter_scopes_and_types(self, within_ops, scopes, types):
        """Filter out ops that are not in `scopes` and not of `types`.

        Args:
          within_ops: an object convertible to a list of `tf.Operation`.
          scopes: a list of scope path.
          types: a list of tf.DataType.
        Return:
          A set of `tf.Operation`.
        """
        ops = set()
        for scope in scopes:
            ops |= set(ge.get_name_scope_ops(within_ops, scope))
        ops |= {op
                for op in within_ops
                if op.type in types}
        return ops

    def _get_forward_walk_ops(self, op, inclusive=True):
        """ A wrapper of `tensorflow.contrib.graph_editor.get_forward_walk_ops`
        """
        if op in self._ops_dict:
            if inclusive:
                return self._ops_dict[op]
            else:
                return list(set(self._ops_dict[op]) - {op})
        else:
            ret = ge.get_forward_walk_ops(op)
            self._ops_dict[op] = ret
            if inclusive:
                return ret
            else:
                return list(set(ret) - {op})

    def run(self, graph=None):
        """Edit the graph by adding swapin and swapout ops.

        Swapin and swapout ops are in the host.

        The graph is modified in-place.

        Return:

          a set of added ops.
        """
        if graph:
            self._graph = graph

        if not self._graph:
            raise ValueError('The dataflow graph is required but has not been'
                             ' provided.')

        self._log_info("Editing model for LMS")
        self._print_configuration()
        start_time = time.time()

        all_ops = ge.make_list_of_op(self._graph)

        self._log_info(
            "The graph has {} ops in total".format(len(all_ops), 1))

        # exclusive ops
        self._excl_ops = self._filter_scopes_and_types(
            all_ops, self._excl_scopes, self._excl_types)
        # inclusive ops
        self._incl_ops = self._filter_scopes_and_types(
            all_ops, self._incl_scopes, self._incl_types)

        # build a topological sort
        self._topo_sort = topos.TOPOS(all_ops)
        self._topo_sort.build()
        self._log_info("Topological sort size: {}".format(
            self._topo_sort.size))
        for i in range(0, self._topo_sort.size):
            self._log_info("[{}]: {}".format(
                i, [op.name for op in self._topo_sort.get_ops(i)]), 1)

        self._do_action(all_ops)

        self._log_info("Added {} ops into the model".format(
            len(self._added_ops)))
        self._log_info(
            "{} tensors will be swapped out, {} tensors will be swapped in".format(
                self._swapout_tensors, self._swapin_tensors))
        self._log_info("Editing model for LMS, took: {} ms".format(
            (time.time()-start_time)*1000))
        return self._added_ops

    def _do_action(self, src_ops):
        """Add swapin and swapout ops for ops that are reachable from `src_ops`.

        Args:
          src_ops: a list of `tf.Operation`
        """
        open_set = Queue.Queue()
        closed_set = set()

        for op in src_ops:
            open_set.put(op)

        while not open_set.empty():
            src_op = open_set.get()

            # get next ops before the graph is changed
            next_ops = set()
            for t in src_op.outputs:
                frontier_ops = set(util.get_consuming_ops(t))
                next_ops |= frontier_ops

            # do action for src_op
            self._insert_swap_nodes(src_op)

            for op in next_ops:
                if op in closed_set:
                    continue
                if op not in open_set.queue:
                    open_set.put(op)

            closed_set.add(src_op)

    def _groupby(self, ops, limit=5):
        """Group `ops` into groups so that topological distance between
        two consecutive ops in a group is within `limit`.

        Args:
          ops: a set of `tf.Operation`.
          limit: a threshold

        Return:
          A list of sets of `tf.Operation`.
        """
        ops_ords = [(op,self._get_order(op)) for op in ops]
        x = sorted([i[1] for i in ops_ords])
        xs = [(i, i) for i in x]

        ys = [xs[0]]
        for i in range(1, len(xs)):
            last = ys[-1]
            curr = xs[i]
            if (curr[0] - last[1] <= limit):
                last = (last[0], curr[1])
                ys[-1] = last
            else:
                ys.append(curr)

        zs = []
        for y in ys:
            gs = set()
            gs = {op[0]
                  for op in ops_ords
                  if (op[1] >= y[0] and op[1] <= y[1])}
            zs.append(gs)
        return zs

    def _insert_swap_nodes(self, src_op):
        """Insert swapin and swapout ops for the given operation into the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`
        """
        self._log_info("Operation: {}".format(src_op), 2)

        # bypass excluded ops
        if src_op in self._excl_ops:
            return

        # if inclusive mode is enabled, only proceed if this op is included
        if self._incl_ops:
            if src_op not in self._incl_ops:
                return

        # do not deal with added ops (swap-out, swap-in ops)
        if src_op in self._added_ops:
            return

        src_op_order = self._get_order(src_op)
        for t in src_op.outputs:
            # candidates are ops whose distance to `src_op` is greater than threshold
            cands = [
                op
                for op in util.get_consuming_ops(t)
                if (self._get_order(op) - src_op_order > self._swapout_threshold)]
            if not cands:
                continue

            self._log_info("Operation: {}, order {}, type {}".format(
                src_op.name, self._get_order(src_op),
                src_op.type), 1)

            # create swap_out node only if there exists a real dest. operation
            swapout_op = self._add_swapout(src_op, t)
            ge.add_control_inputs(swapout_op, src_op)
            self._swapout_tensors = self._swapout_tensors + 1
            self._added_ops.add(swapout_op)

            # create swap_in nodes
            # group near candidates
            cands_grp = self._groupby(cands, self._swapin_groupby)

            for dest_ops in cands_grp:
                # swap_in op
                swapin_op = self._add_swapin(swapout_op, dest_ops, t)
                self._swapin_tensors = self._swapin_tensors + 1
                self._added_ops.add(swapin_op)
                # control dependency -> swap_in
                ops_ords = [(op,self._get_order(op)) for op in dest_ops]
                x = sorted([i[1] for i in ops_ords])[0]  # the earliest op
                dest_op = [op[0] for op in ops_ords if op[1] == x][0]
                self._add_control_dependency(src_op, dest_op, swapin_op)

    def _add_swapout(self, src_op, ts0):
        """Add a swapout operation to the graph to swap out the output tensor `ts0`
        of the operation `src_op`.

        This method does an in-place modification to the graph.

        Example: the graph before and after this method invoked.
        ```
        Before
          (src_op) -> |ts0| -> (dest_op)

        After:
          (src_op) -> |ts0| -> (swapout_op)
          |ts0| -> (dest_op)
        ```

        Args:
          src_op: a `tf.Operation` that produces the tensor `ts0`.
          ts0: a output `tf.Tensor` of `src_op` being swapped out.

        Return:
          A `tf.Operation` newly added to the graph.
        """
        with ops.device(self._cpu_device):
            swap_out = array_ops.identity(ts0, name="lms/swapout")

        # Connect: src-node -> swap-out
        src_svg = ge.sgv(src_op, graph=self._graph)
        src_out_idx = src_svg.output_index(ts0)
        self._connect_ops(src_op, swap_out.op, remap_outputs=True,
                          idx=src_out_idx)
        self._excl_ops.add(swap_out.op)
        self._log_info("Tensor {} will be placed on {}".format(
            ts0.name, self._cpu_device), 1)

        return swap_out.op

    def _add_swapin(self, swapout_op, dest_ops, ts0):
        """Add a swapin operation to the graph. The swapin ops reads
        the output tensor of `swapout_op` and passes it to `dest_ops`,
        replacing the input tensors `ts0` of `dest_ops`.

        This method does an in-place modification to the graph.

        Example: the graph before and after this method invoked.
        ```
        Before
          |ts0| -> (swapout_op)
          |ts0| -> (dest_op)

        After:
          |ts0| -> (swapout_op) -> (swapin_op) -> (dest_op)
        ```

        Args:
          swapout_op: a `tf.Operation` that swapped out the tensor `ts0`.
          dest_ops: a set of `tf.Operation` that will consume the output 
                    tensor of `swapout_op`.
          ts0: a `tf.Tensor` being the original input tensor of `dest_op`.

        Return:
          A `tf.Operation` newly added to the graph.
        """
        with ops.device(self._cpu_device):
            swap_in = array_ops.identity(ts0, name="lms/swapin")

        # Connect: swap_out -> swap_in
        self._connect_ops(swapout_op, swap_in.op)

        # Connect: swap_in -> dest_ops
        for dest_op in dest_ops:
            dest_svg = ge.sgv(dest_op, graph=self._graph)
            input_idx = dest_svg.input_index(ts0)
            self._connect_ops(swap_in.op, dest_op,
                              remap_inputs=True, idx=input_idx)
            self._log_info("Operation {} (order {}) consumes {}".format(
                dest_op.name, self._get_order(dest_op), ts0.name), 1)
        self._excl_ops.add(swap_in.op)

        return swap_in.op

    def _add_control_dependency(self, src_op, dest_op, swapin_op):
        """Find and add a control dependency to the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          dest_op: a `tf.Operation`.
          swapin_op: a `tf.Operation`.
        """
        re = self._do_direct_order(src_op, dest_op, self._swapin_ahead)

        ctrld_op = re[0]
        ctrld_order = re[1]
        if ctrld_op:
            ge.add_control_inputs(swapin_op, ctrld_op)
            self._log_info(
                "Control dependency op {},  order: {}".format(
                    ctrld_op.name, ctrld_order), 1)
        else:
            self._log_info(
                "No control dependency op needed for swap in of op {}.".format(
                    src_op.name), 1)

    def _do_direct_order(self, fw_op, src_op, distance):
        """Find a control dependency operation using topological sort.

        Args:
          fw_op: a `tf.Operation` that has a tensor swapped out.
          bw_op: a `tf.Operation` that consumes a tensor swapped in.
          distance: an `integer`. The distance in the topological order
            between `bw_op` and a candidate for control dependency ops
            must be greater than `distance`.

        Return:
          A tuple of (`tf.Operation`, an `integer`). The first item is
          the control dependency operation that triggers swapping in the input
          tensor of `bw_op`. The second item is the order of the control
          dependency operation in the topological order.
        """
        result_ops = set()

        # offset ordering
        fw_order = self._get_order(fw_op)
        src_order = self._get_order(src_op)

        range_ub = src_order - distance
        range_lb = fw_order + 1

        ctrld_order = -1
        for i in reversed(range(range_lb, range_ub)):
            candidates = self._topo_sort.get_ops(i)
            # on the chain rule path
            candidates = {op
                          for op in candidates
                          if src_op in set(self._get_forward_walk_ops(op))}
            candidates = {op
                          for op in candidates
                          if "/cond/" not in op.name}
            if candidates:
                result_ops |= candidates
                ctrld_order = i
                break

        if result_ops:
            ctrld_op = next(iter(result_ops))
            return (ctrld_op, ctrld_order)
        else:
            return (None, -1)

    def _get_order(self, op):
        return self._topo_sort.get_order(op)

    def _log_info(self, message, level=0):
        """Log debug information.

        Args:
          message: a formatted string.
          level: an `integer`.
        """
        if level == 0 or (self._debug and self._debug_level >= level):
            # Use tf_logging.info instead of print, since print
            # is not thread safe, which can break tests.
            tf_logging.info("[LMS][{}] {}".format(level, message))

    def _print_configuration(self):
        """Print configuration information about LMS.
        """
        self._log_info("swapout_threshold: {}".format(self._swapout_threshold))
        self._log_info("swapin_groupby: {}".format(self._swapin_groupby))
        self._log_info("swapin_ahead: {}".format(self._swapin_ahead))

    def _connect_ops(self, src_op, dest_op, remap_inputs=False,
                     remap_outputs=False, idx=None, disconnect_first=False):
        """A wrapper of `tensorflow.contrib.graph_editor.connect`.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          dest_op: a `tf.Operation`.
          remap_inputs: remap the input of `dest_op` or not.
          remap_outputs: remap the output of `src_op` or not.
          idx: index of input or output tensor.
          disconnect_first: True means the current outputs of sgv0 are
            disconnected.
        """
        src_sgv = ge.sgv(src_op, graph=self._graph)
        dest_sgv = ge.sgv(dest_op, graph=self._graph)
        if remap_outputs:
            src_sgv = src_sgv.remap_outputs([idx])
        if remap_inputs:
            dest_sgv = dest_sgv.remap_inputs([idx])

        ge.connect(src_sgv, dest_sgv, disconnect_first)
