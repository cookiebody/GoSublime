from . import _dbg
from . import gs, gsq, sh
from .margo_agent import MargoAgent
from .margo_common import OutputLogger, TokenCounter
from .margo_render import render, render_src
from .margo_state import State, actions, client_actions, Config, _view_scope_lang, view_is_9o, MgView
from collections import namedtuple
import glob
import os
import shlex
import sublime
import threading
import time

class MargoSingleton(object):
	def __init__(self):
		self._ready = False
		self.out = OutputLogger('margo')
		self.agent_tokens = TokenCounter('agent', format='{}#{:03d}', start=6)
		self.run_tokens = TokenCounter('9o.run')
		self.agent = None
		self.enabled_for_langs = ['*']
		self.state = State()
		self.status = []
		self.output_handler = None
		self._client_actions_handlers = {
			client_actions.Activate: self._handle_act_activate,
			client_actions.Restart: self._handle_act_restart,
			client_actions.Shutdown: self._handle_act_shutdown,
			client_actions.CmdOutput: self._handle_act_output,
		}
		self.file_ids = []

		self._views = {}
		self._view_lock = threading.Lock()
		self._gopath = ''

	def _sync_settings(self):
		old, new = self._gopath, sh.getenv('GOPATH')

		if not new or new == old:
			return

		self._gopath = new

		ag = self.agent
		if not ag or new == ag.gopath:
			return

		self.out.println('Stopping agent. GOPATH changed: `%s` -> `%s`' % (ag.gopath, new))
		self.stop(ag=ag)

	def render(self, rs=None):
		# ST has some locking issues due to its "thread-safe" API
		# don't access things like sublime.active_view() directly

		if rs:
			self.state = rs.state
			cfg = rs.state.config

			self.enabled_for_langs = cfg.enabled_for_langs

			if cfg.override_settings:
				gs._mg_override_settings = cfg.override_settings

		def _render():
			render(view=gs.active_view(), state=self.state, status=self.status)

			if rs:
				self._handle_client_actions(rs)

				if rs.agent and rs.agent is not self.agent:
					rs.agent.stop()

		sublime.set_timeout(_render)


	def _handle_act_activate(self, rs, act):
		gs.focus(act.name or act.path, row=act.row, col=act.col, focus_pat='')

	def _handle_act_restart(self, rs, act):
		self.restart()

	def _handle_act_shutdown(self, rs, act):
		self.stop()

	def _handle_act_output(self, rs, act):
		h = self.output_handler
		if h:
			h(rs, act)

	def _handle_client_actions(self, rs):
		for act in rs.state.client_actions:
			f = self._client_actions_handlers.get(act.action_name)
			if f:
				f(rs, act)
			else:
				self.out.println('Unknown client-action: %s: %s' % (act.action_name, act))

	def render_status(self, *a):
		self.status = list(a)
		self.render()

	def clear_status(self):
		self.render_status()

	def start(self):
		self.restart()

	def restart(self):
		ag = self.agent
		if ag:
			gsq.dispatch('mg.restart-stop', ag.stop)

		self.agent = MargoAgent(self)
		self.agent.start()

	def stop(self, ag=None):
		if not ag or ag is self.agent:
			ag, self.agent = self.agent, None

		if ag:
			ag.stop()

	def enabled(self, view):
		if not self._ready:
			return False

		if '*' in self.enabled_for_langs:
			return True

		_, lang = _view_scope_lang(view, 0)
		return lang in self.enabled_for_langs

	def can_trigger_event(self, view, allow_9o=False):
		_pf=_dbg.pf()

		if view is None:
			return False

		if not self.enabled(view):
			return False

		mgv = self.view(view.id(), view=view)
		if allow_9o and mgv.is_9o:
			return True

		if not mgv.is_file:
			return False

		return True

	def _gs_init(self):
		self._sync_settings()
		gs.sync_settings_callbacks.append(self._sync_settings)

		for w in sublime.windows():
			for v in w.views():
				if v is not None:
					self.view(v.id(), view=v)

		mg._ready = True
		mg.start()

	def view(self, id, view=None):
		with self._view_lock:
			mgv = self._views.get(id)

			if view is not None:
				if mgv is None:
					mgv = MgView(mg=self, view=view)
					self._views[mgv.id] = mgv
				else:
					mgv.sync(view=view)

			return mgv

	def _sync_view(self, event, view):
		if event in ('pre_close', 'close'):
			with self._view_lock:
				self._views.pop(view.id(), None)

			return

		_pf=_dbg.pf(dot=event)

		file_ids = []
		for w in sublime.windows():
			for v in w.views():
				file_ids.append(v.id())

		self.file_ids = file_ids

		self.view(view.id(), view=view)

	def event(self, name, view, handler, args):
		if view is None:
			return None

		_pf=_dbg.pf(dot=name)

		def handle_event(gt=0):
			if gt > 0:
				_pf.gt=gt

			self._sync_view(name, view)

			if not self.can_trigger_event(view):
				return None

			try:
				return handler(*args)
			except Exception:
				gs.error_traceback('mg.event:%s' % handler)
				return None

		blocking = (
			'pre_save',
			'query_completions',
		)

		if name in blocking:
			return handle_event(gt=0.100)

		sublime.set_timeout(handle_event)

	def agent_starting(self, ag):
		if ag is not self.agent:
			return

		self.render_status('starting margo')

	def agent_ready(self, ag):
		if ag is not self.agent:
			return

		self.clear_status()
		self.on_activated(gs.active_view())

	def agent_stopped(self, ag):
		if ag is not self.agent:
			return

		self.agent = None
		self.clear_status()

	def _send_start(self):
		if not self.agent:
			self.start()

	def queue(self, *, actions=[], view=None):
		self._send_start()
		self.agent.queue(actions=actions, view=view)

	def send(self, *, actions=[], cb=None, view=None):
		self._send_start()
		return self.agent.send(actions=actions, cb=cb, view=view)

	def on_new(self, view):
		pass

	def on_pre_close(self, view):
		pass

	def on_query_completions(self, view, prefix, locations):
		_, lang = _view_scope_lang(view, 0)
		if not lang:
			return None

		act = actions.QueryCompletions
		if lang == 'cmd-prompt':
			act = self._cmd_completions_act(view, prefix, locations)
			if not act:
				return None

			view = gs.active_view(win=view.window())
			if view is None:
				return None

		rq = self.send(view=view, actions=[act])
		rs = rq.wait(0.500)
		if not rs:
			self.out.println('aborting QueryCompletions. it did not respond in time')
			return None

		if rs.error:
			self.out.println('completion error: %s: %s' % (act, rs.error))
			return

		if rs.state.view.src:
			self._fmt_rs(
				view=view,
				event='query_completions',
				rq=rq,
				rs=rs,
			)

		cl = [c.entry() for c in rs.state.completions]
		opts = rs.state.config.auto_complete_opts
		return (cl, opts) if opts != 0 else cl

	def _cmd_completions_act(self, view, prefix, locations):
		pos = locations[0]
		line = view.line(pos)
		src = view.substr(line)
		if '#' not in src:
			return None

		i = src.index('#')
		while src[i] == ' ' or src[i] == '#':
			i += 1

		src = src[i:]
		pos = pos - line.begin() - i
		name = ''
		args = shlex.split(src)
		if args:
			name = args[0]
			args = args[1:]

		act = actions.QueryCmdCompletions.copy()
		act['Data'] = {
			'Pos': pos,
			'Src': src,
			'Name': name,
			'Args': args,
		}

		return act

	def on_hover(self, view, pt, zone):
		act = actions.QueryTooltips.copy()
		row, col = view.rowcol(pt)
		act['Data'] = {
			'Row': row,
			'Col': col,
		}
		self.queue(view=view, actions=[act])

	def on_activated(self, view):
		self.queue(view=view, actions=[actions.ViewActivated])

	def on_modified(self, view):
		self.queue(view=view, actions=[actions.ViewModified])

	def on_selection_modified(self, view):
		self.queue(view=view, actions=[actions.ViewPosChanged])

	def fmt(self, view):
		return self._fmt_save(view=view, actions=[actions.ViewFmt], event='fmt', timeout=5.000)

	def on_pre_save(self, view):
		return self._fmt_save(view=view, actions=[actions.ViewPreSave], event='pre_save', timeout=2.000)

	def _fmt_save(self, *, view, actions, event, timeout):
		rq = self.send(view=view, actions=actions)
		rs = rq.wait(timeout)
		self._fmt_rs(
			view=view,
			event=event,
			rq=rq,
			rs=rs,
		)

	def _fmt_rs(self, *, view, event, rq, rs):
		id_nm = '%d: %s' % (view.id(), view.file_name() or view.name())

		if not rs:
			self.out.println('%s timedout on view %s' % (event, id_nm))
			return

		if rs.error:
			self.out.println('%s error in view %s: %s' % (event, id_nm, rs.error))
			return

		req = rq.props.get('View', {})
		res = rs.state.view
		req_name, req_src = req.get('Name'), req.get('Src')
		res_name, res_src = res.name, res.src

		if not res_name or not res_src:
			return

		if req_name != res_name:
			err = '\n'.join((
				'PANIC!!! FMT REQUEST RECEIVED A RESPONSE TO ANOTHER VIEW',
				'PANIC!!! THIS IS A BUG THAT SHOULD BE REPORTED ASAP',
			))
			self.out.println(err)
			gs.show_output('mg.PANIC', err)
			return

		view.run_command('margo_render_src', {'src': res_src})

	def on_post_save(self, view):
		self.queue(view=view, actions=[actions.ViewSaved])

	def on_load(self, view):
		self.queue(view=view, actions=[actions.ViewLoaded])

	def example_extension_file(self):
		return gs.dist_path('src/margo.sh/extension-example/extension-example.go')

	def extension_file(self, install=False):
		src_dir = gs.user_path('src', 'margo')

		def ext_fn():
			l = sorted(glob.glob('%s/*.go' % src_dir))
			return l[0] if l else ''

		fn = ext_fn()
		if fn or not install:
			return fn

		try:
			gs.mkdirp(src_dir)
			with open('%s/margo.go' % src_dir, 'xb') as f:
				s = open(self.example_extension_file(), 'rb').read()
				f.write(s)
		except FileExistsError:
			pass
		except Exception:
			gs.error_traceback('mg.extension_file', status_txt='Cannot create default margo extension package')

		return ext_fn()


mg = MargoSingleton()

def gs_init(_):
	sublime.set_timeout(mg._gs_init)

def gs_fini(_):
	mg.stop()

