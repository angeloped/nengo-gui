from collections import defaultdict
import collections
import logging
import os
import threading
import time
import traceback
import warnings

import json
import nengo
from nengo.utils.compat import iteritems

from nengo_gui import exec_env, user_action
from nengo_gui.components import Component, Value
from nengo_gui.components.slider import OverriddenOutput
from nengo_gui.components.spa_plot import SpaPlot
from nengo_gui.config import Config
from nengo_gui.editor import AceEditor
from nengo_gui.exceptions import NotAttachedError, raise_
from nengo_gui.layout import Layout
from nengo_gui.modal_js import infomodal
from nengo_gui.simcontrol import SimControl
from nengo_gui.threads import RepeatedThread

logger = logging.getLogger(__name__)


class NameFinder(object):

    CLASSES = (nengo.Node, nengo.Ensemble, nengo.Network, nengo.Connection)
    TYPELISTS = ('ensembles', 'nodes', 'connections', 'networks')
    NETIGNORE = ('all_ensembles', 'all_nodes', 'all_connections',
                 'all_networks', 'all_objects', 'all_probes') + TYPELISTS

    def __init__(self, autoprefix="_viz_"):
        self.names = {}
        self.autoprefix = autoprefix
        self.autocount = 0

    def __getitem__(self, obj):
        return self.names[obj]

    def add(self, obj):
        """Add this object to the name finder and return its name.

        This is used for new Components being created (so they can have
        a unique identifier in the .cfg file).
        """
        name = '%s%d' % (self.autoprefix, self.autocount)
        used_names = list(self.names.values())
        while name in used_names:
            self.autocount += 1
            name = '%s%d' % (self.autoprefix, self.autocount)
        self.names[obj] = name
        return name

    def label(self, obj):
        """Return a readable label for an object.

        An important difference between a label and a name is that a label
        does not have to be unique in a namespace.

        If the object has a .label set, this will be used. Otherwise, it
        uses names, which thanks to the NameFinder will be legal
        Python code for referring to the object given the current locals()
        dictionary ("model.ensembles[1]" or "ens" or "model.buffer.state").
        If it has to use names, it will only use the last part of the
        label (after the last "."). This avoids redundancy in nested displays.
        """
        label = obj.label
        if label is None:
            label = self.names[obj]
            if '.' in label:
                label = label.rsplit('.', 1)[1]
        return label

    def update(self, names):
        nets = []
        for k, v in iteritems(names):
            if not k.startswith('_'):
                try:
                    self.names[v] = k
                    if isinstance(v, nengo.Network):
                        nets.append(v)
                except TypeError:
                    pass

        if len(nets) > 1:
            logger.info("More than one top-level model defined.")

        for net in nets:
            self._parse_network(net)

    def _parse_network(self, net):
        net_name = self.names.get(net, None)
        for inst_attr in dir(net):
            private = inst_attr.startswith('_')
            if not private and inst_attr not in self.NETIGNORE:
                attr = getattr(net, inst_attr)
                if isinstance(attr, list):
                    for i, obj in enumerate(attr):
                        if obj not in self.names:
                            n = '%s.%s[%d]' % (net_name, inst_attr, i)
                            self.names[obj] = n
                elif isinstance(attr, self.CLASSES):
                    if attr not in self.names:
                        self.names[attr] = '%s.%s' % (net_name, inst_attr)

        for obj_type in self.TYPELISTS:
            for i, obj in enumerate(getattr(net, obj_type)):
                name = self.names.get(obj, None)
                if name is None:
                    name = '%s.%s[%d]' % (net_name, obj_type, i)
                    self.names[obj] = name

        for n in net.networks:
            self._parse_network(n)


class PageComponents(object):

    def __init__(self):
        self.by_id = {}
        self.by_type = defaultdict(lambda: None)
        self.components = []

    def __iter__(self):
        return iter(self.components)

    def add(self, component, attach=False):
        """Add a new Component to an existing Page."""
        self.by_id[id(component)] = component
        self.components.append(component)
        component.config = self.config[component]

        component.on_page_add()

    def by_type(self, component_class):
        return self.components[component_class]

    def from_locals(self, locals):
        """Get components from a locals dict."""
        for name, obj in iteritems(locals):
            if isinstance(obj, Component):
                self.add(obj)

        # this ensures NetGraph, AceEditor, and SimControl are first
        self.components.sort(key=lambda x: x.component_order)

    def create_javascript(self):
        """Generate the javascript for the current network and layout."""
        assert isinstance(self.components[0], SimControl)
        main = (NetGraph, SimControl, AceEditor)

        main_js = '\n'.join([c.javascript() for c in self.components
                             if isinstance(c, main)])
        component_js = '\n'.join([c.javascript() for c in self.components
                                  if not isinstance(c, main)])
        if not self.context.writeable:
            component_js += "$('#Open_file_button').addClass('deactivated');"
        return main_js, component_js

    def config_change(self, component, new_cfg, old_cfg):
        act = ConfigAction(self,
                           component=component,
                           new_cfg=new_cfg,
                           old_cfg=old_cfg)
        self.undo_stack.append([act])

    def remove_graph(self, component):
        self.undo_stack.append([
            RemoveGraph(self.net_graph, component, self.names.uid(component))])

    def remove_component(self, component):
        """Remove a component from the layout."""
        del self.component_ids[id(component)]
        self.remove_uid(component.uid)
        self.components.remove(component)

    def remove_uid(self, uid):
        """Remove a generated uid (for when a component is removed)."""
        if uid in self.locals:
            obj = self.locals[uid]
            del self.locals[uid]
            del self.names[obj]
        else:
            warnings.warn("remove_uid called on unknown uid: %s" % uid)


class PageNetwork(object):

    def __init__(self, error):
        self.code = None  # the code for the network
        self.locals = None  # the locals() dictionary after executing
        self.filename = None
        self.obj = None  # the nengo.Network object

        # the locals dict for the last time this script was run without errors
        self.last_good_locals = None

        self._set_error = lambda: raise_(NotAttachedError())

    def attach(self, client):
        def set_error(output, line):
            client.dispatch("error.stderr", output=output, line=line)

        self._set_error = set_error

    def build(self, Simulator):
        # Build the simulation
        sim = None
        env = exec_env.ExecutionEnvironment(self.filename, allow_sim=True)
        try:
            with env:
                sim = Simulator(self.obj)
        except Exception:
            self._set_error(output=traceback.format_exc(),
                            line=exec_env.determine_line_number())

        return sim, env.stdout.getvalue()

    def execute(self, code):
        """Run the given code to generate self.network and self.locals.

        The code will be stored in self.code, any output to stdout will
        be a string as self.stdout.
        """
        errored = False

        self.locals = {'__file__': self.filename}

        self.code = code
        self._set_error(output=None, line=None)

        stdout = ''

        env = exec_env.ExecutionEnvironment(self.filename)
        try:
            with env:
                compiled = compile(code, exec_env.compiled_filename, 'exec')
                exec(compiled, self.locals)
        except exec_env.StartedSimulatorException:
            line = exec_env.determine_line_number()
            env.stdout.write('Warning: Simulators cannot be manually '
                             'run inside nengo_gui (line %d)\n' % line)
        except exec_env.StartedGUIException:
            line = exec_env.determine_line_number()
            env.stdout.write('Warning: nengo_gui cannot be run inside '
                             'nengo_gui (line %d)\n' % line)
        except Exception:
            self._set_error(output=traceback.format_exc(),
                            line=exec_env.determine_line_number())
            errored = True
        stdout = env.stdout.getvalue()

        # make sure we've defined a nengo.Network
        self.obj = self.locals.get('model', None)
        if not isinstance(self.obj, nengo.Network):
            if not errored:
                self._set_error(
                    output='Must declare a nengo.Network called "model"',
                    line=len(code.split('\n')))
                errored = True
            self.obj = None

        if not errored:
            self.last_good_locals = self.locals

        return stdout

    def load(self, filename):
        try:
            with open(filename) as f:
                code = f.read()
            self.filename = filename
        except IOError:
            code = ''

        return self.execute(code)


class PageConfig(object):

    def __init__(self):
        self.filename = None
        self.save_needed = False
        self.save_time = None  # time of last config file save
        self.save_period = 2.0  # minimum time between saves

    def clear(self):
        if os.path.isfile(self.filename_cfg):
            os.remove(self.filename_cfg)

    def load(self, net):
        """Load the .cfg file"""
        config = Config()
        net.locals['_gui_config'] = config
        if os.path.exists(self.filename):
            with open(self.filename) as f:
                config_code = f.readlines()
            for line in config_code:
                try:
                    exec(line, net.locals)
                except Exception:
                    # TODO:
                    # if self.gui.interactive:
                    logger.debug('error parsing config: %s', line)

        # make sure the required Components exist
        if '_gui_sim_control' not in net.locals:
            net.locals['_gui_sim_control'] = SimControl()
        if '_gui_net_graph' not in net.locals:
            net.locals['_gui_net_graph'] = NetGraph()
        if '_gui_ace_editor' not in net.locals:
            # TODO: general editor
            net.locals['_gui_ace_editor'] = self.editor_class()

        if net.network is not None:
            if config[net.network].pos is None:
                config[net.network].pos = (0, 0)
            if config[net.network].size is None:
                config[net.network].size = (1.0, 1.0)

        for k, v in net.locals.items():
            if isinstance(v, Component):
                self.default_labels[v] = k
                # TODO: use components.add(
                v.attach(page=self, config=config[v], uid=k)

        return config

    def save(self, lazy=False, force=False):
        """Write the .cfg file to disk.

        Parameters
        ----------
        lazy : bool
            If True, then only save if it has been more than config_save_time
            since the last save and if config_save_needed
        force : bool
            If True, then always save right now
        """
        if not force and not self.config_save_needed:
            return

        now_time = time.time()
        if not force and lazy and self.config_save_time is not None:
            if (now_time - self.config_save_time) < self.config_save_period:
                return

        with self.lock:
            self.config_save_time = now_time
            self.config_save_needed = False
            try:
                with open(self.filename_cfg, 'w') as f:
                    f.write(self.config.dumps(uids=self.default_labels))
            except IOError:
                print("Could not save %s; permission denied" %
                      self.filename_cfg)

    def modified(self):
        """Set a flag that the config file should be saved."""
        self.save_needed = True


class NetGraph(object):
    """Handles computations and communications for NetGraph on the JS side.

    Communicates to all NetGraph components for creation, deletion and
    manipulation.
    """

    RELOAD_EVERY = 0.5  # How often to poll for reload

    def __init__(self):

        # this lock ensures safety between check_for_reload() and update_code()
        self.code_lock = threading.Lock()

        self.new_code = None
        self.layout = None

        self.to_be_expanded = collections.deque()
        self.networks_to_search = []

        self.undo_stack = []
        self.redo_stack = []

        self.uids = {}
        self.parents = {}
        self.initialized_pan_and_zoom = False

        self.config = PageConfig()
        self.components = PageComponents()

        self.names = NameFinder()

        self.net = PageNetwork()

        self.filethread = RepeatedThread(self.RELOAD_EVERY, self._check_file)
        self.filethread.start()  # TODO: defer until after load?

    def attach(self, client):
        # When first attaching, send the pan and zoom
        pan = self.config[self.net.obj].pos
        pan = (0, 0) if pan is None else pan
        zoom = self.config[self.net.obj].size
        zoom = 1.0 if zoom is None else zoom[0]
        client.send("netgraph.pan", pan=pan)
        client.send("netgraph.zoom", zoom=zoom)

        client.bind("netgraph.expand")(self.act_expand)
        client.bind("netgraph.collapse")(self.act_collapse)
        client.bind("netgraph.pan")(self.act_pan)
        client.bind("netgraph.zoom")(self.act_zoom)
        client.bind("netgraph.create_modal")(self.act_create_modal)

        @client.bind("netgraph.action")
        def action(action, **kwargs):
            if action == "expand":
                act = user_action.ExpandCollapse(self, expand=True, **kwargs)
            elif action == "collapse":
                act = user_action.ExpandCollapse(self, expand=False, **kwargs)
            elif action == "create_graph":
                act = user_action.CreateGraph(self, **kwargs)
            elif action == "pos":
                act = user_action.Pos(self, **kwargs)
            elif action == "size":
                act = user_action.Size(self, **kwargs)
            elif action == "pos_size":
                act = user_action.PosSize(self, **kwargs)
            elif action == "feedforward_layout":
                act = user_action.FeedforwardLayout(self, **kwargs)
            elif action == "config":
                act = user_action.ConfigAction(self, **kwargs)
            else:
                act = user_action.Action(self, **kwargs)

            self.undo_stack.append([act])
            del self.redo_stack[:]

        @client.bind("netgraph.undo")
        def undo(undo):
            if undo == "1":
                self.undo()
            else:
                self.redo()

        # if len(self.to_be_expanded) > 0:
        #     with self.page.lock:
        #         network = self.to_be_expanded.popleft()
        #         self.expand_network(network, client)

    # TODO: These should be done as part of loading the model

    # def attach(self, page, config):
    #     super(NetGraph, self).attach(page, config)
    #     self.layout = Layout(page.net.obj)
    #     self.to_be_expanded.append(page.net.obj)
    #     self.networks_to_search.append(page.net.obj)

    #     try:
    #         self.last_modify_time = os.path.getmtime(page.net.filename)
    #     except (OSError, TypeError):
    #         self.last_modify_time = None

    def _check_file(self):
        if self.page.filename is not None:
            try:
                t = os.path.getmtime(self.page.filename)
                if self.last_modify_time is None or self.last_modify_time < t:
                    self.reload()
                    self.last_modify_time = t
            except OSError:
                pass

        with self.code_lock:
            new_code = self.new_code
            # the lock is in case update_code() is called between these lines
            self.new_code = None

        if new_code is not None:
            self.reload(code=new_code)

    def update_code(self, code):
        """Set new version of code to display."""
        with self.code_lock:
            self.new_code = code

    def reload(self, code=None):
        """Called when new code has been detected
        checks that the page is not currently being used
        and thus can be updated"""
        with self.page.lock:
            self._reload(code=code)

    def _reload(self, code=None):
        """Loads and executes the code, removing old items,
        updating changed items
        and adding new ones"""

        old_locals = self.page.last_good_locals
        # TODO: ???
        old_default_labels = self.page.default_labels

        if code is None:
            with open(self.page.filename) as f:
                code = f.read()
            if self.page.code == code:
                # don't re-execute the identical code
                return
            else:
                # send the new code to the client
                self.page.editor.update_code(code)

        self.page.execute(code)

        if self.page.error is not None:
            return

        name_finder = NameFinder(self.page.locals, self.page.model)

        self.networks_to_search = [self.page.model]
        self.parents = {}

        removed_uids = {}
        rebuilt_objects = []

        # for each item in the old model, find the matching new item
        # for Nodes, Ensembles, and Networks, this means to find the item
        # with the same uid.  For Connections, we don't really have a uid,
        # so we use the uids of the pre and post objects.
        for uid, old_item in nengo.utils.compat.iteritems(dict(self.uids)):
            try:
                new_item = eval(uid, self.page.locals)
            except:
                new_item = None

            # check to make sure the new item's uid is the same as the
            # old item.  This is to catch situations where an old uid
            # happens to still refer to something in the new model, but that's
            # not the normal uid for that item.  For example, the uid
            # "ensembles[0]" might still refer to something even after that
            # ensemble is removed.
            new_uid = name_finder[new_item]
            if new_uid != uid:
                new_item = None

            same_class = False
            for cls in (nengo.Ensemble, nengo.Node,
                        nengo.Network, nengo.Connection):
                if isinstance(new_item, cls) and isinstance(old_item, cls):
                    same_class = True
                    break

            # find reasons to delete the object.  Any deleted object will
            # be recreated, so try to keep this to a minimum
            keep_object = True
            if new_item is None:
                keep_object = False
            elif not same_class:
                # don't allow changing classes
                keep_object = False
            elif (self.get_extra_info(new_item) !=
                  self.get_extra_info(old_item)):
                keep_object = False

            if not keep_object:
                self.to_be_sent.append(dict(
                    type='remove', uid=uid))
                del self.uids[uid]
                removed_uids[old_item] = uid
                rebuilt_objects.append(uid)
            else:
                # fix aspects of the item that may have changed
                if self._reload_update_item(uid, old_item, new_item,
                                            name_finder):
                    # something has changed about this object, so rebuild
                    # the components that use it
                    rebuilt_objects.append(uid)

                self.uids[uid] = new_item

        self.to_be_expanded.append(self.page.model)

        self.page.name_finder = name_finder
        self.page.default_labels = name_finder.known_name
        self.page.config = self.page.load_config()
        self.page.uid_prefix_counter = {}
        self.layout = Layout(self.page.model)
        self.page.code = code

        orphan_components = []
        rebuild_components = []

        # items that are shown in components, but not currently displayed
        #  in the NetGraph (i.e. things that are inside collapsed
        #  Networks, but whose values are being shown in a graph)
        collapsed_items = []

        # remove graphs no longer associated to NetgraphItems
        removed_items = list(removed_uids.values())
        for c in self.page.components[:]:
            for item in c.code_python_args(old_default_labels):
                if item not in self.uids and item not in collapsed_items:

                    # item is a python string that is an argument to the
                    # constructor for the Component.  So it could be 'a',
                    # 'model.ensembles[3]', 'True', or even 'target=a'.
                    # We need to evaluate this string in the context of the
                    # locals dictionary and see what object it refers to
                    # so we can determine whether to rebuild this component.
                    #
                    # The following lambda should do this, handling both
                    # the normal argument case and the keyword argument case.
                    safe_eval = ('(lambda *a, **b: '
                                 'list(a) + list(b.values()))(%s)[0]')

                    # this Component depends on an item inside a collapsed
                    #  Network, so we need to check if that component has
                    #  changed or been removed
                    old_obj = eval(safe_eval % item, old_locals)

                    try:
                        new_obj = eval(safe_eval % item, self.page.locals)
                    except:
                        # the object this Component depends on no longer exists
                        new_obj = None

                    if new_obj is None:
                        removed_items.append(item)
                    elif not isinstance(new_obj, old_obj.__class__):
                        rebuilt_objects.append(item)
                    elif (self.get_extra_info(new_obj) !=
                          self.get_extra_info(old_obj)):
                        rebuilt_objects.append(item)

                    # add this to the list of collapsed items, so we
                    # don't recheck it if there's another Component that
                    # also depends on this
                    collapsed_items.append(item)

                if item in rebuilt_objects:
                    self.to_be_sent.append(dict(type='delete_graph',
                                                uid=c.original_id,
                                                notify_server=False))
                    rebuild_components.append(c.uid)
                    self.page.components.remove(c)
                    break
            else:
                for item in c.code_python_args(old_default_labels):
                    if item in removed_items:
                        self.to_be_sent.append(dict(type='delete_graph',
                                                    uid=c.original_id,
                                                    notify_server=False))
                        orphan_components.append(c)
                        break

        components = []
        # the old names for the old components
        component_uids = [c.uid for c in self.page.components]

        for name, obj in list(self.page.locals.items()):
            if isinstance(obj, Component):
                # the object has been removed, so the Component should
                #  be removed as well
                if obj in orphan_components:
                    continue

                # this is a Component that was previously removed,
                #  but is still in the config file, or it has to be
                #  rebuilt, so let's recover it
                if name not in component_uids:
                    self.page.components.add(obj, attach=True)
                    self.to_be_sent.append(dict(type='js',
                                                code=obj.javascript()))
                    components.append(obj)
                    continue

                # otherwise, find the corresponding old Component
                index = component_uids.index(name)
                old_component = self.page.components[index]
                if isinstance(obj, (SimControl, AceEditor, NetGraph)):
                    # just keep these ones
                    components.append(old_component)
                else:
                    # replace these components with the newly generated ones
                    try:
                        self.page.components.add(obj, attach=True)
                        old_component.replace_with = obj
                        obj.original_id = old_component.original_id
                    except:
                        traceback.print_exc()
                        print('failed to recreate plot for %s' % obj)
                    components.append(obj)

        components.sort(key=lambda x: x.component_order)

        self.page.components = components

        # notifies SimControl to pause the simulation
        self.page.changed = True

    def _reload_update_item(self, uid, old_item, new_item, new_name_finder):
        """Tell the client about changes to the item due to reload."""
        changed = False
        if isinstance(old_item, (nengo.Node,
                                 nengo.Ensemble,
                                 nengo.Network)):
            old_label = self.page.names.label(old_item)
            new_label = new_name_finder.label(new_item)

            if old_label != new_label:
                self.to_be_sent.append(dict(
                    type='rename', uid=uid, name=new_label))
                changed = True
            if isinstance(old_item, nengo.Network):
                if self.page.config[old_item].expanded:
                    self.to_be_expanded.append(new_item)
                    changed = True

        elif isinstance(old_item, nengo.Connection):
            old_pre = old_item.pre_obj
            old_post = old_item.post_obj
            new_pre = new_item.pre_obj
            new_post = new_item.post_obj
            if isinstance(old_pre, nengo.ensemble.Neurons):
                old_pre = old_pre.ensemble
            if isinstance(old_post, nengo.connection.LearningRule):
                old_post = old_post.connection.post_obj
            if isinstance(old_post, nengo.ensemble.Neurons):
                old_post = old_post.ensemble
            if isinstance(new_pre, nengo.ensemble.Neurons):
                new_pre = new_pre.ensemble
            if isinstance(new_post, nengo.connection.LearningRule):
                new_post = new_post.connection.post_obj
            if isinstance(new_post, nengo.ensemble.Neurons):
                new_post = new_post.ensemble

            old_pre = self.page.names.uid(old_pre)
            old_post = self.page.names.uid(old_post)
            new_pre = self.page.names.uid(new_pre, names=new_name_finder.names)
            new_post = self.page.names.uid(
                new_post, names=new_name_finder.names)

            if new_pre != old_pre or new_post != old_post:
                # if the connection has changed, tell javascript
                pres = self.get_parents(
                    new_pre,
                    default_labels=new_name_finder.known_name)[:-1]
                posts = self.get_parents(
                    new_post,
                    default_labels=new_name_finder.known_name)[:-1]
                self.to_be_sent.append(dict(
                    type='reconnect', uid=uid,
                    pres=pres, posts=posts))
                changed = True
        return changed

    def get_parents(self, uid, default_labels=None):
        while uid not in self.parents:
            net = self.networks_to_search.pop(0)
            net_uid = self.page.names.uid(net, names=default_labels)
            for n in net.nodes:
                n_uid = self.page.names.uid(n, names=default_labels)
                self.parents[n_uid] = net_uid
            for e in net.ensembles:
                e_uid = self.page.names.uid(e, names=default_labels)
                self.parents[e_uid] = net_uid
            for n in net.networks:
                n_uid = self.page.names.uid(n, names=default_labels)
                self.parents[n_uid] = net_uid
                self.networks_to_search.append(n)
        parents = [uid]
        while parents[-1] in self.parents:
            parents.append(self.parents[parents[-1]])
        return parents

    def modified_config(self):
        self.page.modified_config()

    def undo(self):
        if self.page.undo_stack:
            action = self.page.undo_stack.pop()
            re = []
            for act in action:
                act.undo()
                re.insert(0, act)
            self.page.redo_stack.append(re)

    def redo(self):
        if self.page.redo_stack:
            action = self.page.redo_stack.pop()
            un = []
            for act in action:
                act.apply()
                un.insert(0, act)
            self.page.undo_stack.append(un)

    def act_expand(self, uid):
        net = self.uids[uid]
        self.to_be_expanded.append(net)
        self.page.config[net].expanded = True
        self.modified_config()

    def act_collapse(self, uid):
        net = self.uids[uid]
        self.page.config[net].expanded = False
        self.remove_uids(net)
        self.modified_config()

    def remove_uids(self, net):
        for items in [net.ensembles, net.networks, net.nodes, net.connections]:
            for item in items:
                uid = self.page.names.uid(item)
                if uid in self.uids:
                    del self.uids[uid]
        for n in net.networks:
            self.remove_uids(n)

    def act_pan(self, x, y):
        self.page.config[self.page.model].pos = x, y
        self.modified_config()

    def act_zoom(self, scale, x, y):
        self.page.config[self.page.model].size = scale, scale
        self.page.config[self.page.model].pos = x, y
        self.modified_config()

    def act_create_modal(self, uid, **info):
        js = infomodal(self, uid, **info)
        self.to_be_sent.append(dict(type='js', code=js))

    def expand_network(self, network, client):
        if not self.page.config[network].has_layout:
            pos = self.layout.make_layout(network)
            for obj, layout in pos.items():
                self.page.config[obj].pos = layout['y'], layout['x']
                self.page.config[obj].size = layout['h'] / 2, layout['w'] / 2
            self.page.config[network].has_layout = True

        if network is self.page.model:
            parent = None
        else:
            parent = self.page.names.uid(network)
        for ens in network.ensembles:
            self.create_object(client, ens, type='ens', parent=parent)
        for node in network.nodes:
            self.create_object(client, node, type='node', parent=parent)
        for net in network.networks:
            self.create_object(client, net, type='net', parent=parent)
        for conn in network.connections:
            self.create_connection(client, conn, parent=parent)
        self.page.config[network].expanded = True

    def create_object(self, client, obj, type, parent):
        uid = self.page.names.uid(obj)
        if uid in self.uids:
            return

        pos = self.page.config[obj].pos
        if pos is None:
            import random
            pos = random.uniform(0, 1), random.uniform(0, 1)
            self.page.config[obj].pos = pos
        size = self.page.config[obj].size
        if size is None:
            size = (0.1, 0.1)
            self.page.config[obj].size = size
        label = self.page.names.label(obj)
        self.uids[uid] = obj
        info = dict(uid=uid, label=label, pos=pos, type=type, size=size,
                    parent=parent)
        if type == 'net':
            info['expanded'] = self.page.config[obj].expanded
        info.update(self.get_extra_info(obj))

        client.write_text(json.dumps(info))

    def get_extra_info(self, obj):
        '''Determine helper information for each nengo object.

        This is used by the client side to configure the display.  It is also
        used by the reload() code to determine if a NetGraph object should
        be recreated.
        '''
        info = {}
        if isinstance(obj, nengo.Node):
            if obj.output is None or (
                    isinstance(obj.output, OverriddenOutput)
                    and obj.output.base_output is None):
                info['passthrough'] = True
            if callable(obj.output) and hasattr(obj.output, '_nengo_html_'):
                info['html'] = True
            info['dimensions'] = int(obj.size_out)
        elif isinstance(obj, nengo.Ensemble):
            info['dimensions'] = int(obj.size_out)
            info['n_neurons'] = int(obj.n_neurons)
        elif Value.default_output(obj) is not None:
            info['default_output'] = True

        info['sp_targets'] = (SpaPlot.applicable_targets(obj))
        return info

    def create_connection(self, client, conn, parent):
        uid = self.page.names.uid(conn)
        if uid in self.uids:
            return
        pre = conn.pre_obj
        if isinstance(pre, nengo.ensemble.Neurons):
            pre = pre.ensemble
        post = conn.post_obj
        if isinstance(post, nengo.connection.LearningRule):
            post = post.connection.post
            if isinstance(post, nengo.base.ObjView):
                post = post.obj
        if isinstance(post, nengo.ensemble.Neurons):
            post = post.ensemble
        pre = self.page.names.uid(pre)
        post = self.page.names.uid(post)
        self.uids[uid] = conn
        pres = self.get_parents(pre)[:-1]
        posts = self.get_parents(post)[:-1]
        info = dict(uid=uid, pre=pres, post=posts, type='conn', parent=parent)
        client.write_text(json.dumps(info))