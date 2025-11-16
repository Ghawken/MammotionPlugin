#! /usr/bin/env python
# -*- coding: utf-8 -*-
try:
    import indigo
except ImportError:
    pass

import asyncio
import threading
import time
from datetime import datetime
from typing import Optional

import logging
import logging.handlers
import os
import platform
import sys
import traceback
from os import path

# PyMammotion imports (Cloud + MQTT orchestrated via Mammotion manager)

from pymammotion.mammotion.devices.mammotion import Mammotion
try:
    # HA uses this path
    from pymammotion.utility.constant.device_constant import WorkMode
except Exception:
    # fallback older path
    from pymammotion.utility.constant import WorkMode  # type: ignore

STATUS_INTERVAL_SEC = 15.0
AREA_REQ_COOLDOWN_SEC = 600  # 10 minutes between explicit area-name fetch attempts

class IndigoLogHandler(logging.Handler):
    def __init__(self, display_name, level=logging.NOTSET):
        super().__init__(level)
        self.displayName = display_name

    def emit(self, record):
        logmessage = ""
        is_error = False
        levelno = getattr(record, "levelno", logging.INFO)
        try:
            if self.level <= levelno:
                is_exception = record.exc_info is not None
                if levelno == 5 or levelno == logging.DEBUG:
                    logmessage = "({}:{}:{}): {}".format(path.basename(record.pathname), record.funcName, record.lineno, record.getMessage())
                elif levelno == logging.INFO:
                    logmessage = record.getMessage()
                elif levelno == logging.WARNING:
                    logmessage = record.getMessage()
                elif levelno == logging.ERROR:
                    logmessage = "({}: Function: {}  line: {}):    Error :  Message : {}".format(path.basename(record.pathname), record.funcName, record.lineno, record.getMessage())
                    is_error = True

                if is_exception:
                    logmessage = "({}: Function: {}  line: {}):    Exception :  Message : {}".format(path.basename(record.pathname), record.funcName, record.lineno, record.getMessage())
                    indigo.server.log(message=logmessage, type=self.displayName, isError=is_error, level=levelno)
                    etype, value, tb = record.exc_info
                    tb_string = "".join(traceback.format_tb(tb))
                    indigo.server.log(f"Traceback:\n{tb_string}", type=self.displayName, isError=is_error, level=levelno)
                    indigo.server.log(f"Error in plugin execution:\n\n{traceback.format_exc(30)}", type=self.displayName, isError=is_error, level=levelno)
                    indigo.server.log(f"\nExc_info: {record.exc_info} \nExc_Text: {record.exc_text} \nStack_info: {record.stack_info}", type=self.displayName, isError=is_error, level=levelno)
                    return

                indigo.server.log(message=logmessage, type=self.displayName, isError=is_error, level=levelno)
        except Exception as ex:
            indigo.server.log(f"Error in Logging: {ex}", type=self.displayName, isError=True, level=logging.ERROR)

class Plugin(indigo.PluginBase):
    ########################################
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)

        # Async scaffolding
        self._event_loop = None
        self._async_thread = None

        # Per-device tasks and selected mower names
        self._manager_tasks = {}  # dev.id -> asyncio.Task
        self._periodic_tasks = {}  # dev.id -> asyncio.Task
        self._mower_name = {}  # dev.id -> str
        self._areas_cache = {}  # dev.id -> list of dicts
        self._mgr = {}  # dev.id -> connected Mammotion manager (reuse)
        self._cloud_hooks = {}  # dev.id -> {'msg': callable, 'props': callable}
        self._area_names = {}  # dev_id -> {hash:int -> name:str}
        self._last_forced_state_refresh = {}  # dev_id -> monotonic timestamp
        # In __init__ after your logger setup/runtime maps add:
        self._map_sync_started = {}  # dev_id -> bool (map sync already kicked off)
        self._last_area_req = {}  # dev_id -> monotonic timestamp of last get_area_name_list
        self._area_names_ready = {}  # dev_id -> bool (map.area_name populated)

        # WebRTC (Agora) mini server
        self._webrtc_port = int(self.pluginPrefs.get("webrtcPort", 8787))
        self._webrtc_http_started = False
        # Global (single device) token cache
        self._webrtc_tokens = {}  # {'app_id':..., 'channel':..., 'token':..., 'uid':..., 'expire':...}
        self._webrtc_active_dev_id = None  # last device that refreshed tokens
        self._user_account_id = {}  # dev.id -> int userAccount for MammotionCommand

        # In Plugin.__init__ (after super().__init__):
        self._unknown_area_logged = {}  # dev_id -> set([hashes])
        self._last_map_request = {}  # dev_id -> monotonic timestamp
        self._last_cloud_relogin = {}          # dev_id -> monotonic timestamp
        self._cloud_relogin_in_progress = {}   # dev_id -> bool (true while relogin running)
        self._pending_commands = {}            # dev_id -> list[(key, kwargs)] queued during relogin
        # Logging setup (pattern as in Device Timer / EVSEMaster)
        if hasattr(self, "indigo_log_handler") and self.indigo_log_handler:
            self.logger.removeHandler(self.indigo_log_handler)


        self.logger.setLevel(logging.DEBUG)
        try:
            self.logLevel = int(self.pluginPrefs.get("showDebugLevel", logging.INFO))
            self.fileloglevel = int(self.pluginPrefs.get("showDebugFileLevel", logging.DEBUG))
        except Exception:
            self.logLevel = logging.INFO
            self.fileloglevel = logging.DEBUG

        try:
            if getattr(self, "indigo_log_handler", None):
                self.logger.removeHandler(self.indigo_log_handler)
            self.indigo_log_handler = IndigoLogHandler(plugin_display_name, self.logLevel)
            self.indigo_log_handler.setLevel(self.logLevel)
            self.indigo_log_handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(self.indigo_log_handler)
        except Exception as exc:
            indigo.server.log(f"Failed to create IndigoLogHandler: {exc}", isError=True)

        try:
            #logs_dir = path.join(indigo.server.getInstallFolderPath(), "Logs", "Plugins", self.plugin_id)
            #os.makedirs(logs_dir, exist_ok=True)
            #logfile = path.join(logs_dir, f"{plugin_id}.log")
            #if getattr(self, "plugin_file_handler", None):
            #    self.logger.removeHandler(self.plugin_file_handler)
            #self.plugin_file_handler = logging.handlers.RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3)
            #pfmt = logging.Formatter(
            #    "%(asctime)s.%(msecs)03d\t[%(levelname)8s] %(name)20s.%(funcName)-25s%(message)s",
            #    datefmt="%Y-%m-%d %H:%M:%S",
            #)
            #self.plugin_file_handler.setFormatter(pfmt)
            self.plugin_file_handler.setLevel(self.fileloglevel)
            self.logger.addHandler(self.plugin_file_handler)
        except Exception as exc:
            self.logger.exception(exc)

        self.logger.info("")
        self.logger.info("{0:=^120}".format(" Initializing Mammotion Mower "))
        self.logger.info(f"{'Plugin name:':<28} {plugin_display_name}")
        self.logger.info(f"{'Plugin version:':<28} {plugin_version}")
        self.logger.info(f"{'Plugin ID:':<28} {plugin_id}")
        self.logger.info(f"{'Indigo version:':<28} {indigo.server.version}")
        self.logger.info(f"{'Silicon version:':<28} {platform.machine()}")
        self.logger.info(f"{'Python version:':<28} {sys.version.replace(os.linesep, ' ')}")
        self.logger.info(f"{'Python Directory:':<28} {sys.prefix.replace(os.linesep, ' ')}")

        ##
        # Hook PyMammotion loggers to Indigo handlers so DEBUG from the library shows up
        # --- Diagnostic attach of pymammotion loggers (minimal) ---

        # Route pymammotion logs: logger always DEBUG; handlers control what’s shown
        try:
            # 1. Root logger: make sure we can capture everything to file.
            root_logger = logging.getLogger()
            if self.plugin_file_handler not in root_logger.handlers:
                root_logger.addHandler(self.plugin_file_handler)
            # Keep root permissive so child DEBUG flows (guide: handlers filter)
            if root_logger.level > logging.DEBUG:
                root_logger.setLevel(logging.DEBUG)

            # 2. Plugin logger already has IndigoLogHandler + file handler.
            #    We leave that as-is; no change needed.

            # 3. Library base logger: remove its own handlers (to prevent it blocking propagation),
            #    set NOTSET so it inherits root’s DEBUG, and allow propagation.
            pm = logging.getLogger("pymammotion")
            pm.handlers[:] = []          # strip any handlers library may have added
            pm.setLevel(logging.NOTSET)  # inherit from root (DEBUG)
            pm.propagate = True          # bubble up to root (file handler) AND to any parent chain

            # 4. Emit a test line (will appear once) so you can confirm in file quickly.
            pm.debug("[LOGTEST] pymammotion logger attached (propagate=TRUE -> root)")

            self.logger.debug("Library logging hooked (root captures pymammotion)")
        except Exception as exc:
            self.logger.debug(f"Library logging attach failed: {exc}")

        logging.getLogger("pymammotion").addHandler(self.plugin_file_handler)

        self.logger.info("{0:=^120}".format(" End Initializing "))

    ########################################
    ## Streaming stuff
    def _host_ip_for_links(self) -> str:
        try:
            import socket
            ip = socket.gethostbyname(socket.gethostname())
            return ip or "localhost"
        except Exception:
            return "localhost"

    # New: dynamic Areas menu for Actions.xml
    def areas_menu(self, filter_str: str = "", values_dict: "indigo.Dict" = None, type_id: str = "",
                   target_id: int = 0) -> list:
        """
        Build the Areas list for the action dialog.
        - Never return empty IDs (Indigo rejects them).
        - If fetching, return a sentinel option with a non-empty ID.
        """
        try:
            dev_id = 0
            if target_id:
                dev_id = int(target_id)
            elif values_dict:
                tid = values_dict.get("targetId") or values_dict.get("deviceId") or values_dict.get("devId")
                if tid:
                    try:
                        dev_id = int(tid)
                    except Exception:
                        pass

            if not dev_id:
                return [("no_device", "No device context")]

            cache = self._areas_cache.get(dev_id, [])
            if not cache:
                # Kick off async fetch and return a placeholder (non-empty ID)
                if getattr(self, "_event_loop", None):
                    self._event_loop.call_soon_threadsafe(asyncio.create_task, self._fetch_areas(dev_id))
                return [("fetching", "Fetching areas...")]
            # Build sorted list with safe, non-empty IDs
            items = []
            for item in sorted(cache, key=lambda x: (str(x.get("name") or "")).lower()):
                raw_id = item.get("id")
                name = str(item.get("name") or raw_id)
                # Ensure non-empty string ID
                val = str(raw_id) if raw_id is not None and str(raw_id).strip() else f"id_{abs(hash(name))}"
                items.append((val, name))
            return items if items else [("no_areas", "No areas found")]
        except Exception as exc:
            try:
                self.logger.debug(f"areas_menu error: {exc}")
            except Exception:
                pass
            return [("err", "Error building list")]

    # New: async fetch of areas from the library (best-effort across library versions)
    async def ensure_manual_mode(self, dev_id):
        indigo_dev = indigo.devices.get(dev_id)
        mgr = self._mgr.get(dev_id)
        mower_name = self._mower_name.get(dev_id)
        if not (mgr and mower_name):
            return
        device = mgr.get_device_by_name(mower_name)
        account_id = self._user_account_id.get(dev_id)
        if not (device and account_id):
            return
        from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
        try:
            cmd = MammotionCommand(mower_name, int(account_id)).device_remote_control_with_position(enter_state=1)
            await device.cloud_client.send_cloud_command(device.iot_id, cmd)
            self.logger.info("Entered remote manual control mode")
        except Exception as ex:
            self.logger.error(f"Enter manual control failed: {ex}")

    async def _flush_pending_commands(self, dev_id: int) -> None:
        """
        Execute queued commands (key, kwargs) after a successful relogin.
        Safe no-op if nothing queued.
        """
        # Defensive: make sure the data structure exists
        if not hasattr(self, "_pending_commands"):
            self._pending_commands = {}

        pending = self._pending_commands.get(dev_id, [])
        if not pending:
            return

        self.logger.debug(f"Flushing {len(pending)} queued command(s) for dev {dev_id}")
        # Copy then clear to avoid recursion if a send re-queues
        to_run = list(pending)
        self._pending_commands[dev_id] = []

        for key, kwargs in to_run:
            try:
                await self._send_command(dev_id, key, **(kwargs or {}))
            except Exception as ex:
                self.logger.debug(f"Queued command '{key}' failed after relogin: {ex}")

    async def _fetch_areas(self, dev_id: int):
        """
        Fetch areas from the connected manager. Normalize to [{'id': <str>, 'name': <str>}].
        """
        dev = indigo.devices.get(dev_id)
        if not dev or Mammotion is None:
            return
        try:
            name = self._mower_name.get(dev_id)
            mgr = self._mgr.get(dev_id)
            if not name or not mgr:
                return

            mower = mgr.mower(name)
            areas = []
            try:
                if hasattr(mgr, "get_area_name_list"):
                    raw = await mgr.get_area_name_list(name)
                    if raw:
                        areas = raw
            except Exception as ex:
                self.logger.debug(f"get_area_name_list failed: {ex}")

            try:
                if not areas and mower and hasattr(mower, "get_area_name_list"):
                    raw = await mower.get_area_name_list()
                    if raw:
                        areas = raw
            except Exception as ex:
                self.logger.debug(f"mower.get_area_name_list failed: {ex}")

            # Fallback probes
            if not areas and mower:
                for cand in ("area_list", "areas", "area_names"):
                    try:
                        payload = getattr(mower, cand, None)
                        if payload:
                            areas = payload
                            break
                    except Exception:
                        pass
                if not areas and getattr(mower, "report_data", None):
                    rd = mower.report_data
                    for cand in ("area_list", "areas", "area_names"):
                        try:
                            payload = getattr(rd, cand, None)
                            if payload:
                                areas = payload
                                break
                        except Exception:
                            pass

            # Normalize and de-dup
            norm = []
            if isinstance(areas, dict):
                for k, v in areas.items():
                    _id = str(k)
                    _name = str(v or k)
                    if _id.strip():
                        norm.append({"id": _id, "name": _name})
            elif isinstance(areas, (list, tuple)):
                for it in areas:
                    if isinstance(it, dict):
                        _id = it.get("hash_id") or it.get("id") or it.get("area_hash") or it.get("hash") or it.get(
                            "value") or it.get("key") or it.get("index")
                        _name = it.get("name") or it.get("area_name") or it.get("label") or str(_id)
                        if _id is None:
                            continue
                        _id = str(_id)
                        if not _id.strip():
                            continue
                        norm.append({"id": _id, "name": str(_name)})
                    else:
                        _id = str(it)
                        if _id.strip():
                            norm.append({"id": _id, "name": _id})

            seen = set()
            cleaned = []
            for a in norm:
                sid = a["id"]
                if sid in seen:
                    continue
                seen.add(sid)
                cleaned.append(a)

            self._areas_cache[dev_id] = cleaned
            self.logger.debug(f"Fetched {len(cleaned)} area(s) for '{dev.name}'")
        except Exception as exc:
            self.logger.debug(f"_fetch_areas error for '{dev.name}': {exc}")
    # Indigo async pattern (per user instructions)
    def startup(self):
        self.logger.debug("startup called")
        # Make pymammotion's aiohttp usage compatible with aiohttp>=3.9
        self._install_aiohttp_base_url_shim()
        # Optional but helpful to debug cloud init

        if Mammotion is None:
            self.logger.error("PyMammotion not found. Install with: pip install pymammotion")
        self._event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._event_loop)
        self._async_thread = threading.Thread(target=self._run_async_thread)
        self._async_thread.start()


        ## move to self file - self._start_webrtc_http()
        from webrtc import start_webrtc_http
        start_webrtc_http(self)
        self.logger.info(f"Access Video Stream: http://{self._host_ip_for_links()}:{self._webrtc_port}/webrtc/player")



    def _install_aiohttp_base_url_shim(self) -> None:
        """
        Make PyMammotion's aiohttp usage compatible with aiohttp>=3.9 by treating a first
        positional string argument as base_url. Patch BOTH the symbol inside
        pymammotion.http.http and the global aiohttp.ClientSession so any import path is covered.
        """
        try:
            import aiohttp
            from pymammotion.http import http as pm_http

            original_client_session = aiohttp.ClientSession
            shim_logger = self.logger  # capture to avoid self use in inner fn

            def _patched_client_session(*args, **kwargs):
                used_shim = False
                if args and isinstance(args[0], str) and "base_url" not in kwargs:
                    # Treat the first positional string as base_url
                    kwargs["base_url"] = args[0]
                    args = args[1:]
                    used_shim = True
                try:
                    return original_client_session(*args, **kwargs)
                finally:
                    # One-line, low-noise confirmation that the shim was applied
                    if used_shim:
                        shim_logger.debug("aiohttp shim applied (base_url set)")

            # Patch global and module-local references
            aiohttp.ClientSession = _patched_client_session
            pm_http.ClientSession = _patched_client_session  # type: ignore[attr-defined]
            self.logger.debug("Installed aiohttp base_url compatibility shim for PyMammotion (global + module)")
        except Exception as exc:
            self.logger.error(f"Failed to install aiohttp shim: {exc}")

    # def _start_webrtc_http(self):
    #     """
    #     Start a tiny aiohttp server that exposes:
    #       - POST /webrtc/start : refresh -> join -> fetch tokens, returns {ok, app_id, channel, token, uid, expire}
    #       - POST /webrtc/stop  : leave and clear cache
    #       - GET  /webrtc/tokens.json : cached token bundle (or 404)
    #       - GET  /webrtc/player : simple player page (Play/Stop buttons)
    #     """
    #     if getattr(self, "_webrtc_http_started", False):
    #         return
    #
    #     import asyncio
    #
    #     async def _serve():
    #         try:
    #             from aiohttp import web
    #
    #             async def _json_error(msg, status=400):
    #                 return web.json_response({"ok": False, "error": msg}, status=status)
    #
    #             async def tokens_json(request):
    #                 if not self._webrtc_tokens:
    #                     return _json_error("no tokens (start first)", 404)
    #                 return web.json_response({"ok": True, **self._webrtc_tokens})
    #
    #             async def start_stream(request):
    #                 # Single-device assumption: pick the first enabled/configured mower from this plugin
    #                 dev = None
    #                 for d in indigo.devices.iter("self"):
    #                     if d.enabled and d.configured:
    #                         dev = d
    #                         break
    #                 if dev is None:
    #                     return _json_error("no enabled/configured device found")
    #
    #                 mgr = self._mgr.get(dev.id)
    #                 mower_name = self._mower_name.get(dev.id)
    #                 if not mgr or not mower_name:
    #                     return _json_error("manager/mower not ready (wait for Connected)")
    #
    #                 device = mgr.get_device_by_name(mower_name)
    #                 if not device:
    #                     return _json_error("internal: device wrapper missing")
    #
    #                 # Ensure userAccount (identity)
    #                 account_id = self._user_account_id.get(dev.id)
    #                 if account_id is None:
    #                     try:
    #                         http_resp = getattr(device.mammotion_http, "response", None)
    #                         if http_resp and getattr(http_resp, "data", None):
    #                             ui = getattr(http_resp.data, "userInformation", None)
    #                             if ui and getattr(ui, "userAccount", None) is not None:
    #                                 account_id = int(ui.userAccount)
    #                                 self._user_account_id[dev.id] = account_id
    #                     except Exception:
    #                         pass
    #                 if account_id is None:
    #                     return _json_error("userAccount not available yet")
    #
    #                 # Pre-refresh (like HA)
    #                 try:
    #                     _ = await device.mammotion_http.get_stream_subscription(device.iot_id)
    #                 except Exception:
    #                     pass
    #
    #                 # Join (enter_state=1)
    #                 try:
    #                     from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
    #                     cmd = MammotionCommand(mower_name, int(account_id)).device_agora_join_channel_with_position(
    #                         enter_state=1)
    #                     await device.cloud_client.send_cloud_command(device.iot_id, cmd)
    #                 except Exception as ex:
    #                     return _json_error(f"join failed: {ex}")
    #
    #                 # Small delay then fetch fresh subscription
    #                 try:
    #                     await asyncio.sleep(1.5)
    #                 except Exception:
    #                     pass
    #
    #                 try:
    #                     stream_resp = await device.mammotion_http.get_stream_subscription(device.iot_id)
    #                     raw = stream_resp.data.to_dict() if getattr(stream_resp, "data", None) else {}
    #                 except Exception as ex:
    #                     return _json_error(f"token fetch failed: {ex}")
    #
    #                 # Normalize keys (HA returns appid/channelName/token/uid)
    #                 app_id = raw.get("app_id") or raw.get("appId") or raw.get("appid") or ""
    #                 channel = raw.get("channel") or raw.get("channelName") or raw.get("ch") or ""
    #                 token = raw.get("token") or raw.get("accessToken") or raw.get("agoraToken") or ""
    #                 uid = raw.get("uid") or raw.get("userId") or raw.get("uidStr") or ""
    #                 expire = raw.get("expire") or raw.get("expire_ts") or raw.get("expireTime") or 0
    #                 try:
    #                     expire = int(expire or 0)
    #                 except Exception:
    #                     expire = 0
    #
    #                 self._webrtc_tokens = {
    #                     "app_id": str(app_id),
    #                     "channel": str(channel),
    #                     "token": str(token),
    #                     "uid": str(uid),
    #                     "expire": expire,
    #                 }
    #                 self._webrtc_active_dev_id = dev.id
    #
    #                 # Mirror a few human-readable states (no props writes)
    #                 kv = []
    #                 if "stream_app_id" in dev.states: kv.append({"key": "stream_app_id", "value": str(app_id)})
    #                 if "stream_channel" in dev.states: kv.append({"key": "stream_channel", "value": str(channel)})
    #                 if "stream_token" in dev.states: kv.append(
    #                     {"key": "stream_token", "value": ("set" if token else "")})
    #                 if "stream_uid" in dev.states: kv.append({"key": "stream_uid", "value": str(uid)})
    #                 if "stream_expire" in dev.states: kv.append({"key": "stream_expire", "value": int(expire)})
    #                 if "stream_status" in dev.states: kv.append(
    #                     {"key": "stream_status", "value": ("OK" if app_id and channel and token else "Empty")})
    #                 if kv:
    #                     try:
    #                         dev.updateStatesOnServer(kv)
    #                     except Exception:
    #                         pass
    #
    #                 if not (app_id and channel and token):
    #                     return _json_error("incomplete token bundle (device may not have started publishing yet)")
    #
    #                 return web.json_response({"ok": True, **self._webrtc_tokens})
    #
    #             async def stop_stream(request):
    #                 # leave if we have an active dev
    #                 dev_id = self._webrtc_active_dev_id
    #                 if dev_id:
    #                     try:
    #                         dev = indigo.devices.get(dev_id)
    #                         mgr = self._mgr.get(dev_id)
    #                         mower_name = self._mower_name.get(dev_id)
    #                         if mgr and mower_name and dev:
    #                             device = mgr.get_device_by_name(mower_name)
    #                             account_id = self._user_account_id.get(dev_id)
    #                             if account_id is not None and device:
    #                                 from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
    #                                 cmd = MammotionCommand(mower_name,
    #                                                        int(account_id)).device_agora_join_channel_with_position(
    #                                     enter_state=0)
    #                                 await device.cloud_client.send_cloud_command(device.iot_id, cmd)
    #                     except Exception as ex:
    #                         self.logger.error(f"Stop stream leave failed: {ex}")
    #                 self._webrtc_tokens = {}
    #                 self._webrtc_active_dev_id = None
    #                 return web.json_response({"ok": True})
    #
    #             async def player(request):
    #                 # No f-strings here — braces in CSS/JS are literal and safe.
    #                 html = '''<!doctype html>
    # <html><head>
    # <meta charset="utf-8"/>
    # <meta name="viewport" content="width=device-width,initial-scale=1"/>
    # <title>Mammotion Camera</title>
    # <style>
    # html,body{margin:0;background:#000;color:#fff;font-family:sans-serif}
    # #v{position:absolute;top:0;left:0;width:100%;height:100%;object-fit:contain;background:#000}
    # #ui{position:fixed;top:8px;left:8px;display:flex;gap:6px;z-index:10}
    # button{background:#222;color:#fff;border:1px solid #444;padding:6px 10px;border-radius:4px;cursor:pointer;font-size:13px}
    # button:disabled{opacity:.4;cursor:not-allowed}
    # #status{position:fixed;top:48px;left:8px;background:rgba(0,0,0,.55);padding:6px 10px;border-radius:6px;font-size:12px;line-height:1.4;max-width:320px}
    # </style></head>
    # <body>
    # <div id="ui">
    #   <button id="playBtn">Play</button>
    #   <button id="stopBtn" disabled>Stop</button>
    #   <button id="reloadBtn">Reload</button>
    # </div>
    # <div id="status">Idle.</div>
    # <video id="v" autoplay playsinline controls muted></video>
    # <script>
    # const statusEl=document.getElementById('status');
    # const playBtn=document.getElementById('playBtn');
    # const stopBtn=document.getElementById('stopBtn');
    # const reloadBtn=document.getElementById('reloadBtn');
    # const videoEl=document.getElementById('v');
    # let client=null;
    # function setStatus(msg){statusEl.innerHTML=msg;}
    # async function startAll(){
    #   if (client){setStatus('Already running');return;}
    #   playBtn.disabled=true; setStatus('Starting (join + tokens)...');
    #   try{
    #     const resp=await fetch('/webrtc/start',{method:'POST'});
    #     const data=await resp.json();
    #     if(!data.ok){throw new Error(data.error||'start failed');}
    #     setStatus('Tokens received. Loading Agora SDK...');
    #     await loadSdk();
    #     await joinAgora(data);
    #   }catch(e){
    #     setStatus('Start error: '+e.message);
    #     playBtn.disabled=false;
    #   }
    # }
    # async function loadSdk(){
    #   return new Promise((resolve,reject)=>{
    #     if(window.AgoraRTC) return resolve();
    #     const s=document.createElement('script');
    #     s.src='https://download.agora.io/sdk/release/AgoraRTC_N.js';
    #     s.onload=()=>resolve();
    #     s.onerror=()=>reject(new Error('SDK load failed'));
    #     document.head.appendChild(s);
    #   });
    # }
    # async function joinAgora(t){
    #   if(!window.AgoraRTC) throw new Error('SDK missing');
    #   if(window.AgoraRTC.setLogLevel) AgoraRTC.setLogLevel(4);
    #   if(window.AgoraRTC.disableLogUpload) AgoraRTC.disableLogUpload();
    #   const cfg={mode:'live',codec:'vp8'};
    #   client=AgoraRTC.createClient(cfg);
    #   if(client.setClientRole) await client.setClientRole('host'); // HA parity
    #   client.on('user-published', async(user,mediaType)=>{
    #      try{
    #        await client.subscribe(user,mediaType);
    #        if(mediaType==='video'&&user.videoTrack){
    #           user.videoTrack.play(videoEl);
    #           setStatus('Video subscribed uid='+user.uid);
    #        }
    #        if(mediaType==='audio'&&user.audioTrack){
    #           user.audioTrack.play();
    #        }
    #      }catch(e){setStatus('Subscribe failed: '+e);}
    #   });
    #   client.on('user-unpublished', (user)=>{setStatus('User unpublished uid='+user.uid);});
    #   await client.join(t.app_id,t.channel,t.token||null,t.uid?parseInt(t.uid):null);
    #   setStatus('Joined channel. Awaiting video...');
    #   stopBtn.disabled=false;
    # }
    # async function stopAll(){
    #   stopBtn.disabled=true;
    #   try{
    #     if(client){await client.leave(); client=null;}
    #     await fetch('/webrtc/stop',{method:'POST'});
    #     setStatus('Stopped.');
    #     playBtn.disabled=false;
    #   }catch(e){
    #     setStatus('Stop error: '+e.message);
    #     playBtn.disabled=false;
    #   }
    # }
    # playBtn.addEventListener('click', startAll);
    # stopBtn.addEventListener('click', stopAll);
    # reloadBtn.addEventListener('click', ()=>location.reload());
    # </script>
    # </body></html>'''
    #                 from aiohttp import web
    #                 return web.Response(text=html, content_type="text/html")
    #
    #             app = web.Application()
    #             app.router.add_post("/webrtc/start", start_stream)
    #             app.router.add_post("/webrtc/stop", stop_stream)
    #             app.router.add_get("/webrtc/tokens.json", tokens_json)
    #             app.router.add_get("/webrtc/player", player)
    #
    #             runner = web.AppRunner(app)
    #             await runner.setup()
    #             site = web.TCPSite(runner, "0.0.0.0", self._webrtc_port)
    #             await site.start()
    #             self.logger.info(f"WebRTC server listening on port {self._webrtc_port}")
    #         except Exception as exc:
    #             self.logger.exception(exc)
    #
    #     if not getattr(self, "_event_loop", None):
    #         self.logger.error("Async loop not running; cannot start WebRTC server")
    #         return
    #
    #     self._webrtc_http_started = True
    #     self._event_loop.call_soon_threadsafe(asyncio.create_task, _serve())

    def _is_auth_error(self, ex) -> bool:
        """
        Return True if the exception indicates Mammotion cloud auth is invalid/expired.
        Consolidated: works with explicit code fields, args, and known exception types /
        messages (parallels HA's EXPIRED_CREDENTIAL_EXCEPTIONS).
        """
        try:
            # direct 'code' attribute
            code = getattr(ex, "code", None)
            if isinstance(code, int) and code in (29002, 29003, 460):
                return True

            # first arg numeric
            if ex.args and isinstance(ex.args[0], int) and ex.args[0] in (29002, 29003, 460):
                return True

            # HA-style exceptions: CheckSessionException / SetupException / UnauthorizedException
            import pymammotion
            try:
                from pymammotion.aliyun.cloud_gateway import CheckSessionException, SetupException
            except Exception:
                CheckSessionException = SetupException = ()
            try:
                from pymammotion.http.model.http import UnauthorizedException
            except Exception:
                UnauthorizedException = ()
            if isinstance(ex, (CheckSessionException, SetupException, UnauthorizedException)):
                return True
        except Exception:
            pass

        s = str(ex) or ""
        return any(
            t in s
            for t in (
                "identityId is blank",   # 29003 text
                "iotToken is blank",     # 460 text
                "code:29003",
                "code:29002",
                "code:460",
                " 29003",
                " 29002",
                " 460",
                "token invalid",
                "SignatureDoesNotMatch",
            )
        )

    def shutdown(self):
        self.logger.debug("shutdown called")

    def _run_async_thread(self):
        self.logger.debug("_run_async_thread starting")
        self._event_loop.create_task(self._async_start())
        self._event_loop.run_until_complete(self._async_stop())
        self._event_loop.close()

    async def _async_start(self):
        self.logger.debug("_async_start")
        self.logger.debug("Starting event loop and setting up any connections")

    async def _async_stop(self):
        while True:
            await asyncio.sleep(5.0)
            if self.stopThread:
                # Cancel per-device tasks
                for t in list(self._periodic_tasks.values()):
                    if t and not t.done():
                        t.cancel()
                for t in list(self._manager_tasks.values()):
                    if t and not t.done():
                        t.cancel()
                break


    def _force_refresh_states(self, dev_id: int, delay: float = 0.3, min_interval: float = 0.8):
        """
        Schedule a quick _refresh_states run after an optional delay.
        min_interval prevents spamming (movement pulses).
        """
        now = time.monotonic()
        last = self._last_forced_state_refresh.get(dev_id, 0.0)
        if (now - last) < min_interval:
            return
        self._last_forced_state_refresh[dev_id] = now

        loop = getattr(self, "_event_loop", None)
        if not loop:
            # Fallback: best-effort direct call
            try:
                if delay > 0:
                    time.sleep(delay)
                indigo.server.queueFuture(
                    lambda: asyncio.run_coroutine_threadsafe(self._refresh_states(dev_id), asyncio.get_event_loop()))
            except Exception:
                pass
            return

        async def _do():
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._refresh_states(dev_id)
            except Exception:
                self.logger.exception("force refresh failed")

        try:
            loop.call_soon_threadsafe(asyncio.create_task, _do())
        except Exception:
            self.logger.exception("schedule force refresh failed")
    ########################################
    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):
        """
        Validate only account/password. Return errors as indigo.Dict to satisfy Indigo's
        UiValidate converter (prevents: No registered converter... CXmlDict).
        """
        try:
            errors = indigo.Dict()  # Use Indigo dict for the errors map

            account = (values_dict.get("account") or "").strip()
            password = (values_dict.get("password") or "").strip()

            if not account:
                errors["account"] = "Please enter your Mammotion account (email or account id)."
            if not password:
                errors["password"] = "Please enter your Mammotion account password."

            if len(errors) > 0:
                return (False, errors, values_dict)

            return (True, values_dict)
        except Exception as exc:
            self.logger.exception(exc)
            errors = indigo.Dict()
            errors["password"] = f"Validation error: {exc}"
            return (False, errors, values_dict)

    def closedPrefsConfigUi(self, values_dict: indigo.Dict, user_cancelled: bool) -> None:
        self.logger.debug(
            f"closedPluginConfigUi called with values_dict: {values_dict} and user_cancelled: {user_cancelled}")
        if user_cancelled:
            return
        import logging as _l
        try:
            # Persist
            self.pluginPrefs["showDebugLevel"] = int(values_dict.get("showDebugLevel", _l.INFO))
            self.pluginPrefs["showDebugFileLevel"] = int(values_dict.get("showDebugFileLevel", _l.DEBUG))
            self.pluginPrefs["debugLibrary"] = bool(values_dict.get("debugLibrary", False))
            indigo.server.savePluginPrefs()

            # Apply
            self.logLevel = int(values_dict.get("showDebugLevel", _l.INFO))
            self.fileloglevel = int(values_dict.get("showDebugFileLevel", _l.DEBUG))
            self.indigo_log_handler.setLevel(self.logLevel)
            self.plugin_file_handler.setLevel(self.fileloglevel)

            # PyMammotion logger levels + handlers
            try:
                self.indigo_log_handler.setLevel(self.logLevel)
                self.plugin_file_handler.setLevel(self.fileloglevel)
                # Re-confirm propagation (in case another part changed it)
                pm = logging.getLogger("pymammotion")
                pm.propagate = True
                pm.setLevel(logging.NOTSET)
                self.logger.debug(
                    f"Logging prefs applied: Indigo={logging.getLevelName(self.indigo_log_handler.level)}, "
                    f"File={logging.getLevelName(self.plugin_file_handler.level)}"
                )
            except Exception as exc:
                self.logger.debug(f"Pref logging update failed: {exc}")

        except Exception as exc:
            self.logger.exception(exc)

    ########################################
    def deviceStartComm(self, dev):
        if dev.deviceTypeId != "mammotionMower":
            return
        # Purge legacy props if present (no impact if absent)
        try:
            props = dict(dev.pluginProps or {})
            changed = False
            for k in ("connectionType", "pin"):
                if k in props:
                    del props[k]
                    changed = True
            if changed:
                dev.replacePluginPropsOnServer(props)
        except Exception:
            pass

        self.logger.info(f"Starting Mammotion device '{dev.name}'")
        dev.stateListOrDisplayStateIdChanged()
        try:
            dev.updateStateImageOnServer(indigo.kStateImageSel.Auto)
        except Exception:
            pass
        self._set_basic(dev.id, connected=False, status="Initializing...")

        if self._event_loop:
            def _ensure():
                if dev.id in self._manager_tasks and not self._manager_tasks[dev.id].done():
                    return
                self._manager_tasks[dev.id] = asyncio.create_task(self._session_manager(dev.id))

            self._event_loop.call_soon_threadsafe(_ensure)

    def deviceStopComm(self, dev):
        if dev.deviceTypeId != "mammotionMower":
            return
        self.logger.info(f"Stopping Mammotion device '{dev.name}'")

        if self._event_loop:
            def _cancel():
                pt = self._periodic_tasks.pop(dev.id, None)
                if pt and not pt.done():
                    pt.cancel()
                mt = self._manager_tasks.pop(dev.id, None)
                if mt and not mt.done():
                    mt.cancel()

            self._event_loop.call_soon_threadsafe(_cancel)

        # Drop the connected manager for this device
        try:
            self._mgr.pop(dev.id, None)
        except Exception:
            pass

        self._set_connected(dev.id, False)
        self._set_auth(dev.id, False)

    ########################################
    # Relay-like control mapping (On = Start; Off = Dock; Toggle)
    def actionControlDevice(self, action, dev):
        if dev.deviceTypeId != "mammotionMower":
            return
        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.start_mowing_action(None, dev)
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.return_home_action(None, dev)
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            mowing = bool(dev.states.get("mowing", False))
            if mowing:
                self.pause_mowing_action(None, dev)
            else:
                self.start_mowing_action(None, dev)

    def start_mowing_action(self, action, dev):
        self.logger.info(f"Start mowing requested for '{dev.name}'")
        self._schedule(dev.id, self._send_command(dev.id, "start_job"))

    def pause_mowing_action(self, action, dev):
        self.logger.info(f"Pause mowing requested for '{dev.name}'")
        self._schedule(dev.id, self._send_command(dev.id, "pause_execute_task"))

    def return_home_action(self, action, dev):
        self.logger.info(f"Return to dock requested for '{dev.name}'")
        self._schedule(dev.id, self._send_command(dev.id, "return_to_dock"))

    ########################################
    # Async orchestration
    def _schedule(self, dev_id, coro):
        if not self._event_loop:
            self.logger.error("Async loop not running.")
            return
        self._event_loop.call_soon_threadsafe(asyncio.create_task, coro)

    # ... existing imports and class scaffolding ...

    # Add to __init__ runtime maps if not already present:
    # self._mgr: dict[int, Mammotion]      # connected manager per Indigo dev
    # self._mower_name: dict[int, str]     # chosen mower name per Indigo dev
    # self._periodic_tasks: dict[int, asyncio.Task]
    # self._manager_tasks: dict[int, asyncio.Task]

    # ========== Session manager: login, store manager, enable cloud, bind callbacks ==========
    async def _session_manager(self, dev_id: int):
        backoff = 2.0
        while not self.stopThread:
            dev = indigo.devices.get(dev_id)
            if not dev or not dev.enabled or not dev.configured:
                await asyncio.sleep(2.0)
                continue

            if Mammotion is None:
                self._set_basic(dev_id, connected=False, status="PyMammotion not installed")
                await asyncio.sleep(10.0)
                continue

            props = dev.pluginProps or {}
            account = (props.get("account") or "").strip()
            password = (props.get("password") or "").strip()
            name_hint = (props.get("deviceName") or "").strip()

            if not account or not password:
                miss = []
                if not account:
                    miss.append("account")
                if not password:
                    miss.append("password")
                self._set_basic(dev_id, connected=False, status=f"Missing {', '.join(miss)}")
                await asyncio.sleep(10.0)
                continue

            try:
                self._set_basic(dev_id, connected=False, status="Connecting...")
                mgr = Mammotion()
                self.logger.debug(f"Login begin for account '{account}' (library login_and_initiate_cloud)")
                await mgr.login_and_initiate_cloud(account, password)

                # Store manager for reuse everywhere
                self._mgr[dev_id] = mgr

                # Pick mower by name (or first bound device)
                mower_name = await self._resolve_mower_name(mgr, name_hint)
                if not mower_name:
                    self._set_status(dev_id, "No mower found")
                    raise RuntimeError("No mower found on account")
                self._mower_name[dev_id] = mower_name
                self.logger.info(f"'{dev.name}' now Connected.  Controlling mower name: {mower_name}")

                # inside async def _session_manager(self, dev_id: int): after:
                # self._mower_name[dev_id] = mower_name

                try:
                    # Prefer the device-scoped HTTP client like HA does
                    device = mgr.get_device_by_name(mower_name)
                    account_id = None
                    if device and getattr(device, "mammotion_http", None):
                        http_resp = getattr(device.mammotion_http, "response", None)
                        if http_resp and getattr(http_resp, "data", None) and getattr(http_resp.data, "userInformation",
                                                                                      None):
                            ua = getattr(http_resp.data.userInformation, "userAccount", None)
                            if ua is not None:
                                account_id = int(ua)
                    self._user_account_id[dev_id] = account_id
                    if account_id is not None:
                        self.logger.debug(f"Cached Mammotion userAccount for '{mower_name}': {account_id}")
                    else:
                        self.logger.warning(
                            f"Could not read Mammotion userAccount for '{mower_name}'. Camera join will fail until it’s available.")
                except Exception as ex:
                    self._user_account_id[dev_id] = None
                    self.logger.debug(f"Reading Mammotion userAccount failed: {ex}")

                # Enable cloud for this device and bind callbacks like HA
                await self._enable_cloud_and_bind(dev_id, mgr, mower_name)

                # Prime telemetry: get_report_cfg, then request_iot_sys
                await self._prime_reporting(dev_id, mgr)

                self._set_connected(dev_id, True)
                self._set_auth(dev_id, True)
                self._set_status(dev_id, "Connected")

                # Start periodic refresh (lightweight) and keepalive for reports
                if dev_id in self._periodic_tasks and not self._periodic_tasks[dev_id].done():
                    self._periodic_tasks[dev_id].cancel()
                self._periodic_tasks[dev_id] = asyncio.create_task(self._periodic_status(dev_id))

                # Idle while connected
                while not self.stopThread and self._mgr.get(dev_id) is mgr:
                    await asyncio.sleep(2.0)

            except asyncio.CancelledError:
                break
            except Exception as ex:
                self.logger.error(f"Connection error for '{dev.name}': {ex}")
                self._set_basic(dev_id, connected=False, status=f"Error: {ex}")
            finally:
                pt = self._periodic_tasks.pop(dev_id, None)
                if pt and not pt.done():
                    pt.cancel()
                self._mgr.pop(dev_id, None)
                self._set_connected(dev_id, False)
                self._set_auth(dev_id, False)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
##

    # Helper (place inside Plugin class)
    def _maybe_request_map(self, dev_id: int, min_interval: float = 30.0):
        """
        Best-effort gentle map refresh trigger – only if we haven't already
        tried recently. Keep it minimal to avoid library churn.
        """
        import time
        now = time.monotonic()
        last = self._last_map_request.get(dev_id, 0.0)
        if (now - last) < min_interval:
            return
        self._last_map_request[dev_id] = now
        try:
            # If you have a dedicated "start_map_sync" or similar command wrapper,
            # call that. Otherwise send the underlying library command if exposed.
            # Example (adjust to your existing send wrapper):
            self.logger.debug(f"Requesting map sync for dev {dev_id}")
            self._schedule(dev_id, self._send_command(dev_id, "start_map_sync"))
        except Exception:
            pass
    # ========== Match HA: plain get_report_cfg then request_iot_sys with enums ==========
    async def _prime_reporting(self, dev_id: int, mgr):
        name = self._mower_name.get(dev_id)
        if not name or not mgr:
            return
        try:
            await mgr.send_command(name, "get_report_cfg")
            try:
                from pymammotion.proto import RptAct, RptInfoType
                await mgr.send_command_with_args(
                    name,
                    "request_iot_sys",
                    rpt_act=RptAct.RPT_START,
                    rpt_info_type=[
                        RptInfoType.RIT_DEV_STA,
                        RptInfoType.RIT_DEV_LOCAL,
                        RptInfoType.RIT_WORK,
                        RptInfoType.RIT_MAINTAIN,
                        RptInfoType.RIT_BASESTATION_INFO,
                        RptInfoType.RIT_VIO,
                    ],
                    timeout=10000,
                    period=3000,
                    no_change_period=4000,
                    count=0,
                )
            except Exception as ex:
                self.logger.debug(f"request_iot_sys enums failed, trying minimal start: {ex}")
                await mgr.send_command_with_args(
                    name,
                    "request_iot_sys",
                    timeout=10000,
                    period=3000,
                    no_change_period=4000,
                    count=0,
                )
            # Give MQTT a tick then refresh so HA-style report_data.* fields populate
            await asyncio.sleep(1.0)
            await self._refresh_states(dev_id)
            self._set_status(dev_id, "Telemetry primed")
            self.logger.debug(f"Telemetry primed for '{name}' via get_report_cfg + request_iot_sys")
        except Exception as ex:
            self.logger.debug(f"Telemetry prime failed for '{name}': {ex}")

    async def _wait_map_names_then_refresh(self, dev_id: int, mgr, mower_name: str):
        """
        Wait up to ~20s for area_name list to populate, then refresh states.
        Avoids racing get_area_name_list so 'work_area' can resolve to a name.
        """
        try:
            for _ in range(20):
                await asyncio.sleep(1.0)
                mowing_device = mgr.mower(mower_name)
                if not mowing_device:
                    continue
                m = getattr(mowing_device, "map", None)
                names = getattr(m, "area_name", []) if m else []
                if names and len(names) > 0:
                    self._schedule_state_refresh(dev_id)
                    return
            # No names yet; still refresh once so at least hash shows
            self._schedule_state_refresh(dev_id)
        except Exception:
            pass

    async def _enable_cloud_and_bind(self, dev_id: int, mgr, mower_name: str):
        """
        Enable cloud updates and bind callbacks like HA; start map sync once and throttle area-name fetches.
        """
        device = None
        try:
            if hasattr(mgr, "get_device_by_name"):
                device = mgr.get_device_by_name(mower_name)
        except Exception:
            device = None

        if not device:
            self.logger.debug(f"No device manager wrapper for '{mower_name}', continuing without direct callbacks")
            return

        # Enable scheduled updates
        try:
            device.state.enabled = True
            if device.cloud and getattr(device.cloud, "stopped", False):
                await device.cloud.start()
        except Exception as ex:
            self.logger.debug(f"Enable cloud failed for '{mower_name}': {ex}")

        # Bind a single cloud notification callback – on any inbound, refresh states
        try:
            cloud = getattr(device, "cloud", None)
            if cloud and hasattr(cloud, "set_notification_callback"):
                def _cloud_notify(res):
                    # Also detect when map names arrive so we don’t keep requesting
                    try:
                        md = mgr.mower(mower_name)
                        m = getattr(md, "map", None)
                        names = getattr(m, "area_name", []) if m else []
                        if names and len(names) > 0:
                            if not self._area_names_ready.get(dev_id, False):
                                self.logger.debug(f"Area names now present (count={len(names)}) for '{mower_name}'")
                            self._area_names_ready[dev_id] = True
                    except Exception:
                        pass
                    self._schedule_state_refresh(dev_id)
                    if hasattr(cloud, "set_error_callback"):
                        async def _cloud_error(exc):
                            if self._is_auth_error(exc):
                                self.logger.warning(
                                    f"Cloud error (auth) for '{mower_name}', scheduling re-login"
                                )
                                self._set_auth(dev_id, False)
                                try:
                                    await self._cloud_relogin_once(dev_id)
                                except Exception as re:
                                    self.logger.debug(f"_cloud_error: relogin failed for '{mower_name}': {re}")

                        cloud.set_error_callback(self._async_wrap(_cloud_error))

            else:
                self.logger.debug("cloud.set_notification_callback not available on this build")
        except Exception as ex:
            self.logger.debug(f"Bind cloud notification failed: {ex}")

        # Bind state_manager callbacks (properties/status/device events)
        try:
            sm = getattr(device, "state_manager", None)
            if sm and hasattr(sm, "properties_callback"):
                sm.properties_callback.add_subscribers(lambda p: self._schedule_state_refresh(dev_id))
            if sm and hasattr(sm, "status_callback"):
                sm.status_callback.add_subscribers(lambda s: self._schedule_state_refresh(dev_id))
            if sm and hasattr(sm, "device_event_callback"):
                sm.device_event_callback.add_subscribers(lambda e: self._schedule_state_refresh(dev_id))
        except Exception as ex:
            self.logger.debug(f"Bind state_manager callbacks failed: {ex}")

        # Kick map sync ONCE per Indigo device
        try:
            if not self._map_sync_started.get(dev_id, False):
                if hasattr(mgr, "start_map_sync"):
                    self._map_sync_started[dev_id] = True
                    self._area_names_ready[dev_id] = False
                    self.logger.debug(f"Starting map sync for '{mower_name}' (first time)")
                    await mgr.start_map_sync(mower_name)
                else:
                    self.logger.debug("start_map_sync not available on this build")
            else:
                self.logger.debug(f"Map sync already started for '{mower_name}', skipping re-run")
        except Exception as ex:
            self.logger.debug(f"Map sync failed to start: {ex}")

        # Throttled area-name fetch (if names not present yet)
        try:
            import time as _time
            if not self._area_names_ready.get(dev_id, False):
                last = float(self._last_area_req.get(dev_id, 0.0))
                now = _time.monotonic()
                if (now - last) >= AREA_REQ_COOLDOWN_SEC:
                    self._last_area_req[dev_id] = now
                    self.logger.debug(f"Requesting area name list for '{mower_name}' (cooldown ok)")
                    try:
                        # Preferred signature
                        await mgr.send_command_with_args("get_area_name_list", device_id=device.iot_id)
                    except Exception:
                        # Fallback signature
                        try:
                            await mgr.send_command(mower_name, "get_area_name_list")
                        except Exception as ex2:
                            self.logger.debug(f"get_area_name_list failed: {ex2}")
                else:
                    self.logger.debug(
                        f"Skipping get_area_name_list for '{mower_name}' (cooldown {(AREA_REQ_COOLDOWN_SEC - int(now - last))}s left)")
            else:
                self.logger.debug(f"Area names already present for '{mower_name}', not requesting list")
        except Exception as ex:
            self.logger.debug(f"Area-name request scheduling failed: {ex}")

    # ========== Schedule a safe refresh from callbacks ==========
    def _schedule_state_refresh(self, dev_id: int):
        if not self._event_loop:
            return
        self._event_loop.call_soon_threadsafe(asyncio.create_task, self._refresh_states(dev_id))

    # ========== Lightweight periodic: refresh + keep report stream warm ==========
    async def _periodic_status(self, dev_id: int):
        # Shorter interval while working; otherwise slower. Keep get_report_cfg warm every ~60s.
        keepalive_counter = 0
        try:
            while not self.stopThread:
                await self._refresh_states(dev_id)

                keepalive_counter = (keepalive_counter + 1) % 6  # every ~6 cycles
                if keepalive_counter == 0:
                    try:
                        mgr = self._mgr.get(dev_id)
                        name = self._mower_name.get(dev_id)
                        if mgr and name:
                            await mgr.send_command(name, "get_report_cfg")
                    except Exception:
                        pass

                # If working, poll faster (like HA WORKING_INTERVAL); otherwise default
                try:
                    dev = indigo.devices.get(dev_id)
                    wm = (dev.states.get("state_raw") or "").strip()
                    # crude: treat 'MODE_WORKING' or '19/13' combos as active
                    sleep_s = 5 if ("WORKING" in dev.states.get("work_mode", "")) else 30
                except Exception:
                    sleep_s = 30
                await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return

    # ========== Refresh states (reuse connected manager) – unchanged mapping logic ok ==========
    def _resolve_area_name(self, mowing_device, area_hash: int) -> str | None:
        """
        Mirror HA get_area_entity_name: prefer map.area_name lookup by hash.
        Fallbacks to "area <hash>" or None if hash is 0.
        """
        if not area_hash or area_hash in (0, "0", ""):
            return None
        try:
            m = getattr(mowing_device, "map", None)
            if not m:
                return f"area {area_hash}"
            # Try direct name list first
            names = getattr(m, "area_name", []) or []
            for an in names:
                if getattr(an, "hash", None) == area_hash:
                    nm = getattr(an, "name", None)
                    return nm if nm else f"area {area_hash}"
            # Try via area frames
            area = getattr(m, "area", {}) or {}
            area_entry = area.get(area_hash) if isinstance(area, dict) else None
            if area_entry and getattr(area_entry, "data", None):
                frame0 = area_entry.data[0] if len(area_entry.data) > 0 else None
                if frame0 is not None:
                    for an in names:
                        if getattr(an, "hash", None) == getattr(frame0, "hash", None):
                            nm = getattr(an, "name", None)
                            return nm if nm else f"area {area_hash}"
        except Exception:
            pass
        return f"area {area_hash}"

    async def _resolve_mower_name(self, mgr: "Mammotion", name_hint: str) -> Optional[str]:
        """
        Return a mower name:
        - exact name_hint if present and exists
        - else first device name in the device manager
        """
        try:
            if name_hint:
                dev_mgr = mgr.get_device_by_name(name_hint)
                if dev_mgr:
                    return name_hint
            # fall back to first known device
            # device_manager.devices is a dict[str, MammotionMixedDeviceManager]
            # use getattr to avoid attribute errors if internals change
            device_manager = getattr(mgr, "device_manager", None)
            devices_map = getattr(device_manager, "devices", {}) if device_manager else {}
            if isinstance(devices_map, dict) and devices_map:
                # Choose first mower-like device
                for key in devices_map.keys():
                    return key
        except Exception:
            return None
        return None
##
    async def _refresh_states(self, dev_id: int):
        """
        Update Indigo states and a combined, human-readable status line, e.g.:
          - "Mowing <area>, remaining <n>%, battery <b>%"
          - "Returning to Dock (battery <b>%)"
          - "Docked and Charging"
          - "Docked, Not Charging"
          - "Idle, Not Charging"
          - "Error: <message>"
        Plus robust blades_on detection (cut_knife_ctrl | blade_status) and optional blade_rpm.
        """
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        name = self._mower_name.get(dev_id)
        mgr = self._mgr.get(dev_id)
        if not name or not mgr:
            return

        try:
            mowing_device = mgr.mower(name)
            if mowing_device is None:
                self._set_status(dev_id, "Waiting for mower data...")
                self._set_connected(dev_id, True)
                self.logger.debug(f"_refresh_states: mower() returned None for '{name}'")
                return

            report_data = getattr(mowing_device, "report_data", None)
            dev_status = getattr(report_data, "dev", None) if report_data else None
            work = getattr(report_data, "work", None) if report_data else None
            connect = getattr(report_data, "connect", None) if report_data else None
            rtk = getattr(report_data, "rtk", None) if report_data else None
            local_status = getattr(report_data, "local", None) if report_data else None
            maintenance = getattr(report_data, "maintenance", None) if report_data else None
            vision_info = getattr(report_data, "vision_info", None) if report_data else None
            mower_state = getattr(mowing_device, "mower_state", None)
            location = getattr(mowing_device, "location", None)
            map_obj = getattr(mowing_device, "map", None)

            # Presence snapshot
            try:
                names_cnt = len(getattr(map_obj, "area_name", []) or [])
            except Exception:
                names_cnt = 0
            try:
                areas_cnt = len(getattr(map_obj, "area", {}) or {})
            except Exception:
                areas_cnt = 0
            wz = getattr(location, "work_zone", None) if location else None

            # Connected flag
            try:
                self._set_connected(dev_id, bool(getattr(mowing_device, "online", True)))
            except Exception:
                self._set_connected(dev_id, True)

            allowed = set(dev.states.keys())

            kv = []
            from datetime import datetime as _dt
            kv.append({"key": "last_update", "value": _dt.now().strftime("%Y-%m-%d %H:%M:%S")})
            kv.append({"key": "status_text", "value": "OK"})

            # Work mode / raw
            sys_status = getattr(dev_status, "sys_status", None)
            mode_name = ""
            mode_int = None
            try:
                if sys_status is not None:
                    mode_int = int(sys_status)
                    # Prefer device_constant mapping
                    from pymammotion.utility.constant.device_constant import device_mode as _device_mode
                    mode_name = str(_device_mode(mode_int))
            except Exception:
                try:
                    from pymammotion.utility.constant import WorkMode
                    if sys_status is not None:
                        mode_int = int(sys_status)
                        mode_name = WorkMode(mode_int).name.replace("_", " ").title()
                except Exception:
                    mode_name = f"Status {sys_status}" if sys_status is not None else ""

            if "work_mode" in allowed and mode_name:
                kv.append({"key": "work_mode", "value": mode_name})
            if "state_raw" in allowed and sys_status is not None:
                kv.append({"key": "state_raw", "value": str(sys_status)})

            # Battery / charge
            batt_pct = None
            try:
                batt = getattr(dev_status, "battery_val", None)
                if batt is not None:
                    batt_pct = int(batt)
                if "battery_percent" in allowed and batt is not None:
                    kv.append({"key": "battery_percent", "value": int(batt)})
            except Exception:
                self.logger.exception("battery_percent failed")

            charging = None
            try:
                ch_state = getattr(dev_status, "charge_state", None)
                if ch_state is not None:
                    charging = bool(int(ch_state) != 0)
                if "charging" in allowed and ch_state is not None:
                    kv.append({"key": "charging", "value": bool(int(ch_state) != 0)})
            except Exception:
                self.logger.exception("charging decode failed")

            # Blades: robust detection (cut_knife_ctrl first; fall back to blade_status)
            # Simplified blades_on: working mode = True
            blades_on_val = None
            try:
                from pymammotion.utility.constant.device_constant import WorkMode as _WM2
                if mode_int is not None and _WM2 and mode_int == int(_WM2.MODE_WORKING):
                    blades_on_val = True
                else:
                    blades_on_val = False
                if "blades_on" in allowed:
                    kv.append({"key": "blades_on", "value": blades_on_val})
            except Exception:
                pass

            # blade_rpm if provided (not all firmwares provide this)
            try:
                rpm_val = getattr(mower_state, "blade_rpm", None)
                if rpm_val is not None and "blade_rpm" in allowed:
                    kv.append({"key": "blade_rpm", "value": int(rpm_val)})
            except Exception:
                # Do not log at error level; it's optional
                pass

            # Model/FW/Rain
            try:
                if "model_name" in allowed and getattr(mower_state, "model", None):
                    kv.append({"key": "model_name", "value": str(getattr(mower_state, "model", ""))})
                if "fw_version" in allowed and getattr(mower_state, "swversion", None):
                    kv.append({"key": "fw_version", "value": str(getattr(mower_state, "swversion", ""))})
                if "rain_detected" in allowed:
                    kv.append({"key": "rain_detected", "value": bool(getattr(mower_state, "rain_detection", False))})
            except Exception:
                self.logger.exception("mower_state block failed")

            # RSSI
            try:
                if connect is not None and "wifi_rssi" in allowed and getattr(connect, "wifi_rssi", None) is not None:
                    kv.append({"key": "wifi_rssi", "value": int(connect.wifi_rssi)})
            except Exception:
                self.logger.exception("connect RSSI failed")

            # Work progress and blade height
            progress_pct = None
            try:
                if work is not None:
                    area_raw = getattr(work, "area", 0) or 0
                    progress_pct = int((area_raw >> 16) & 0xFFFF)
                    if "progress_percent" in allowed:
                        kv.append({"key": "progress_percent", "value": progress_pct})
                    if "blade_height_mm" in allowed and getattr(work, "knife_height", None) is not None:
                        kv.append({"key": "blade_height_mm", "value": int(getattr(work, "knife_height", 0))})
            except Exception:
                self.logger.exception("work block failed")

            # RTK snapshot
            try:
                if rtk is not None:
                    if "satellites_total" in allowed and getattr(rtk, "gps_stars", None) is not None:
                        kv.append({"key": "satellites_total", "value": int(rtk.gps_stars)})
                    if getattr(rtk, "co_view_stars", None) is not None:
                        try:
                            l2 = int((rtk.co_view_stars >> 8) & 0xFF)
                            if "satellites_l2" in allowed:
                                kv.append({"key": "satellites_l2", "value": l2})
                        except Exception:
                            self.logger.exception("satellite split failed")
                    if "rtk_age" in allowed and getattr(rtk, "age", None) is not None:
                        kv.append({"key": "rtk_age", "value": int(getattr(rtk, "age", 0))})
            except Exception:
                self.logger.exception("rtk block failed")

            # Position sources (local->vision->work.path)
            try:
                src = local_status if local_status is not None else None
                pos_x_val = pos_y_val = lat_std_val = lon_std_val = pos_type_val = pos_level_val = toward_val = None

                if src is not None:
                    def _num(val):
                        return None if val is None else float(val)

                    pos_x_val = _num(getattr(src, "pos_x", None))
                    pos_y_val = _num(getattr(src, "pos_y", None))
                    lat_std_val = _num(getattr(src, "lat_std", None))
                    lon_std_val = _num(getattr(src, "lon_std", None))
                    pos_type_val = getattr(src, "pos_type", None)
                    pos_level_val = getattr(src, "pos_level", None)
                    toward_val = getattr(src, "toward", None)
                else:
                    if vision_info is not None:
                        try:
                            vx = getattr(vision_info, "x", None)
                            vy = getattr(vision_info, "y", None)
                            vh = getattr(vision_info, "heading", None)
                            if vx is not None and vy is not None:
                                pos_x_val = float(vx)
                                pos_y_val = float(vy)
                            if vh is not None:
                                toward_val = int(round(float(vh)))
                        except Exception:
                            self.logger.exception("vision_info fallback parse failed")
                    if pos_x_val is None and work is not None:
                        try:
                            wpx = getattr(work, "path_pos_x", None)
                            wpy = getattr(work, "path_pos_y", None)
                            if wpx is not None and wpy is not None:
                                pos_x_val = float(wpx) / 1000.0
                                pos_y_val = float(wpy) / 1000.0
                        except Exception:
                            self.logger.exception("work.path_pos fallback parse failed")
                    if pos_type_val is None and location is not None:
                        try:
                            pos_type_val = getattr(location, "position_type", None)
                        except Exception:
                            pass
                    if pos_level_val is None and rtk is not None:
                        try:
                            pos_level_val = getattr(rtk, "pos_level", None)
                        except Exception:
                            pass

                if pos_x_val is not None and "pos_x" in allowed:
                    kv.append({"key": "pos_x", "value": pos_x_val})
                if pos_y_val is not None and "pos_y" in allowed:
                    kv.append({"key": "pos_y", "value": pos_y_val})
                if lat_std_val is not None and "lat_std" in allowed:
                    kv.append({"key": "lat_std", "value": lat_std_val})
                    if "lat_std.ui" in allowed:
                        kv.append({"key": "lat_std.ui", "value": f"{lat_std_val:.3f}"})
                if lon_std_val is not None and "lon_std" in allowed:
                    kv.append({"key": "lon_std", "value": lon_std_val})
                    if "lon_std.ui" in allowed:
                        kv.append({"key": "lon_std.ui", "value": f"{lon_std_val:.3f}"})
                if pos_type_val is not None and "pos_type" in allowed:
                    kv.append({"key": "pos_type", "value": int(pos_type_val)})
                if pos_level_val is not None and "pos_level" in allowed:
                    kv.append({"key": "pos_level", "value": int(pos_level_val)})
                if toward_val is not None and "toward" in allowed:
                    kv.append({"key": "toward", "value": int(toward_val)})
            except Exception:
                self.logger.exception("position block failed")

            # GPS lat/lon preference RTK -> device
            try:
                import math
                rtk_lat_r = getattr(getattr(location, "RTK", None), "latitude", None) if location else None
                rtk_lon_r = getattr(getattr(location, "RTK", None), "longitude", None) if location else None
                dev_lat_r = getattr(getattr(location, "device", None), "latitude", None) if location else None
                dev_lon_r = getattr(getattr(location, "device", None), "longitude", None) if location else None

                def _rad_to_deg(val):
                    try:
                        return float(val) * 180.0 / math.pi
                    except Exception:
                        return None

                lat_deg = _rad_to_deg(rtk_lat_r) if rtk_lat_r not in (None, 0.0) else None
                lon_deg = _rad_to_deg(rtk_lon_r) if rtk_lon_r not in (None, 0.0) else None
                if lat_deg is None:
                    lat_deg = _rad_to_deg(dev_lat_r) if dev_lat_r is not None else None
                if lon_deg is None:
                    lon_deg = _rad_to_deg(dev_lon_r) if dev_lon_r is not None else None

                if lat_deg is not None and "gps_lat" in allowed:
                    kv.append({"key": "gps_lat", "value": lat_deg})
                if lon_deg is not None and "gps_lon" in allowed:
                    kv.append({"key": "gps_lon", "value": lon_deg})
            except Exception:
                self.logger.exception("gps lat/lon fallback failed")

            # zone_hash + area_name
            area_name_val = None
            try:
                if location is not None:
                    hz = getattr(location, "work_zone", None)
                    if hz is not None:
                        hz_int = int(hz)
                        if "zone_hash" in allowed:
                            kv.append({"key": "zone_hash", "value": hz_int})
                        if "area_name" in allowed:
                            if hz_int == 0:
                                area_name_val = "Not working"
                            else:
                                nm = None
                                names = getattr(map_obj, "area_name", []) if map_obj else []
                                for an in (names or []):
                                    if getattr(an, "hash", None) == hz_int:
                                        nm = getattr(an, "name", None) or f"area {hz_int}"
                                        break
                                if nm is None:
                                    area_tbl = getattr(map_obj, "area", {}) if map_obj else {}
                                    aentry = area_tbl.get(hz_int) if isinstance(area_tbl, dict) else None
                                    if aentry and getattr(aentry, "data", None):
                                        frame0 = aentry.data[0] if len(aentry.data) > 0 else None
                                        if frame0 is not None:
                                            for an in (names or []):
                                                if getattr(an, "hash", None) == getattr(frame0, "hash", None):
                                                    nm = getattr(an, "name", None) or f"area {hz_int}"
                                                    break
                                # ... inside the zone_hash / area_name resolution block, replace the final else branch:

                                if nm is None:
                                    # Fallback – suppress repetitive spam
                                    dev_logged = self._unknown_area_logged.setdefault(dev_id, set())
                                    if hz_int not in dev_logged:
                                        self.logger.debug(
                                            f"_refresh_states: first miss resolving area name hash={hz_int}; "
                                            f"area_name_count={names_cnt}, area_count={areas_cnt}"
                                        )
                                        dev_logged.add(hz_int)
                                        # Try (once per interval) to pull map names
                                        self._maybe_request_map(dev_id)
                                    # Provide a readable fallback (short form: last 6 digits)
                                    short_tail = str(hz_int)[-6:]
                                    nm = f"Area {short_tail}"
                                area_name_val = str(nm)
                                kv.append({"key": "area_name", "value": area_name_val})

            except Exception:
                self.logger.exception("work area resolution failed")

            # Summary toggles (basic flags)
            try:
                if "state_summary" in allowed:
                    parts = []
                    if mode_name:
                        parts.append(mode_name)
                    if blades_on_val:
                        parts.append("Blades On")
                    if charging:
                        parts.append("Charging")
                    if parts:
                        kv.append({"key": "state_summary", "value": " | ".join(parts)})

                # Booleans for convenience
                mowing = None
                try:
                    from pymammotion.utility.constant import WorkMode as _WM
                    if mode_int is not None:
                        mowing = (mode_int == int(_WM.MODE_WORKING))
                except Exception:
                    mowing = bool(blades_on_val)
                if mowing is not None and "onOffState" in allowed:
                    kv.append({"key": "onOffState", "value": bool(mowing)})
                if mowing is not None and "mowing" in allowed:
                    kv.append({"key": "mowing", "value": bool(mowing)})

                docked = False
                if "docked" in allowed:
                    try:
                        from pymammotion.utility.constant import WorkMode as _WM2
                        # Consider 'ready' + charging as docked (heuristic)
                        docked = bool(charging) and (mode_int is not None and mode_int == int(_WM2.MODE_READY))
                    except Exception:
                        docked = False
                    kv.append({"key": "docked", "value": docked})
            except Exception:
                self.logger.exception("summary block failed")

            # Combined, human-readable status (with explicit Returning handling)
            try:
                # Error detection from existing states/alarms
                error_msg = None
                try:
                    existing_err = dev.states.get("error_text", "")
                    if existing_err:
                        error_msg = str(existing_err)
                except Exception:
                    pass
                try:
                    tilt = bool(getattr(mower_state, "tilt_alarm", False))
                    lift = bool(getattr(mower_state, "lift_alarm", False))
                    if error_msg is None and (tilt or lift):
                        bits = []
                        if tilt: bits.append("Tilt alarm")
                        if lift: bits.append("Lift alarm")
                        error_msg = ", ".join(bits)
                except Exception:
                    pass

                remaining_pct = None
                if isinstance(progress_pct, int):
                    remaining_pct = max(0, 100 - progress_pct)
                    if "progress_remaining" in allowed:
                        kv.append({"key": "progress_remaining", "value": int(remaining_pct)})

                # Booleans for logic
                mowing_val = None
                try:
                    mowing_val = next((d["value"] for d in kv if d["key"] == "mowing"), mowing)
                except Exception:
                    mowing_val = mowing
                docked_val = None
                try:
                    docked_val = next((d["value"] for d in kv if d["key"] == "docked"), False)
                except Exception:
                    docked_val = False
                charging_val = charging

                # Area label
                area_label = area_name_val
                if not area_label:
                    try:
                        zh = next((d["value"] for d in kv if d["key"] == "zone_hash"), None)
                        if zh not in (None, 0):
                            area_label = f"area {int(zh)}"
                    except Exception:
                        pass

                # Battery text
                batt_text = f"{batt_pct}%" if batt_pct is not None else "--"

                # Explicit Returning handling
                is_returning = False
                try:
                    from pymammotion.utility.constant import WorkMode as _WMX
                    if mode_int is not None:
                        is_returning = (mode_int == int(_WMX.MODE_RETURNING))
                except Exception:
                    # Fallback textual match
                    is_returning = isinstance(mode_name, str) and ("Returning" in mode_name or "Return" in mode_name)

                combined = None
                if error_msg:
                    combined = f"Error: {error_msg}"
                elif is_returning:
                    combined = f"Returning to Dock (battery {batt_text})"
                elif mowing_val:
                    rem_text = f"{remaining_pct}%" if remaining_pct is not None else "--"
                    if not area_label:
                        area_label = "area"
                    combined = f"Mowing {area_label}, remaining {rem_text}, battery {batt_text}"
                elif docked_val and bool(charging_val):
                    combined = f"Docked and Charging (battery {batt_text})"
                elif docked_val and not bool(charging_val):
                    combined = f"Docked, Not Charging (battery {batt_text})"
                else:
                    # Idle if not mowing and not docked
                    if bool(charging_val):
                        combined = f"Charging (battery {batt_text})"
                    else:
                        combined = f"Idle, Not Charging (battery {batt_text})"

                if combined and "status_combined" in allowed:
                    kv.append({"key": "status_combined", "value": combined})
            except Exception:
                self.logger.exception("combined status build failed")

            # Push updates
            try:
                kv_safe = [d for d in kv if d.get("key") in allowed]
                if len(kv_safe) != len(kv):
                    missing = [d["key"] for d in kv if d["key"] not in allowed]
                    self.logger.debug(f"_refresh_states: skipped undefined Indigo states for '{dev.name}': {missing}")
                if kv_safe:
                    dev.updateStatesOnServer(kv_safe)
            except Exception:
                self.logger.exception("updateStatesOnServer failed")

        except Exception:
            self._set_status(dev_id, "Poll error")
            self.logger.exception("_refresh_states top-level failure")

    # inside Plugin class
    async def _cloud_relogin_once(self, dev_id: int, min_interval: float = 60.0) -> None:
        """
        Re-login / refresh Mammotion cloud session for this Indigo device.
        Prefer the CloudIOTGateway.check_or_refresh_session() (HA-style),
        fall back to Mammotion.login_and_initiate_cloud() only if necessary.
        Debounced via min_interval and a per-dev in-progress flag.
        """
        import time
        now = time.monotonic()
        last = self._last_cloud_relogin.get(dev_id, 0.0)
        if (now - last) < min_interval:
            return
        self._last_cloud_relogin[dev_id] = now

        if self._cloud_relogin_in_progress.get(dev_id, False):
            return
        self._cloud_relogin_in_progress[dev_id] = True

        dev = indigo.devices.get(dev_id)
        mgr = self._mgr.get(dev_id)
        if not dev or not mgr:
            self._cloud_relogin_in_progress[dev_id] = False
            return

        props = dev.pluginProps or {}
        account = (props.get("account") or "").strip()
        password = (props.get("password") or "").strip()
        if not account or not password:
            self.logger.debug(f"cloud_relogin: missing credentials for dev {dev_id}")
            self._cloud_relogin_in_progress[dev_id] = False
            return

        try:
            self.logger.debug(f"cloud_relogin: attempting refresh for '{dev.name}'")

            # 1. Try the per-device cloud_client first, HA style
            cloud_refreshed = False
            try:
                mower_name = self._mower_name.get(dev_id)
                device = mgr.get_device_by_name(mower_name) if mower_name else None
                cloud_client = getattr(device, "cloud_client", None)
                if cloud_client and hasattr(cloud_client, "check_or_refresh_session"):
                    await cloud_client.check_or_refresh_session()
                    cloud_refreshed = True
                    self.logger.debug(f"cloud_relogin: cloud_client.check_or_refresh_session() OK for '{dev.name}'")
            except Exception as ex:
                self.logger.debug(f"cloud_relogin: cloud_client refresh failed for '{dev.name}': {ex}")

            # 2. Fallback to full login if gateway refresh not possible or failed
            if not cloud_refreshed:
                self.logger.debug(f"cloud_relogin: falling back to login_and_initiate_cloud for '{dev.name}'")
                await mgr.login_and_initiate_cloud(account, password)

            # 3. Re-enable cloud + callbacks + telemetry (same as before)
            try:
                mower_name = self._mower_name.get(dev_id)
                if mower_name:
                    device = mgr.get_device_by_name(mower_name)
                    cloud = getattr(device, "cloud", None)
                    if cloud and getattr(cloud, "stopped", False):
                        await cloud.start()
                    await self._enable_cloud_and_bind(dev_id, mgr, mower_name)
            except Exception as ex2:
                self.logger.debug(f"cloud_relogin: re-bind failed for '{dev.name}': {ex2}")

            try:
                await asyncio.sleep(1.0)
                await self._prime_reporting(dev_id, mgr)
            except Exception as ex2:
                self.logger.debug(f"cloud_relogin: post-login prime failed for '{dev.name}': {ex2}")

            await self._flush_pending_commands(dev_id)
            # Mark auth as good again
            self._set_auth(dev_id, True)

        except Exception as ex:
            self.logger.debug(f"cloud_relogin: failed for '{dev.name}': {ex}")
            # do not clear pending commands; they can retry on next success
        finally:
            self._cloud_relogin_in_progress[dev_id] = False

    # _send_command stripped (movement verbs no sync/map; add sys_status logging)
    # inside Plugin class
    async def _send_command(self, dev_id: int, key: str, **kwargs):
        """
        Central command dispatcher (coordinator-like):
        - Sends Mammotion commands via the current manager.
        - On auth error, triggers cloud refresh and retries once.

        """
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        name = self._mower_name.get(dev_id)
        mgr = self._mgr.get(dev_id)
        if not name or not mgr:
            self._set_status(dev_id, "No mower selected")
            return

        import contextlib

        nosync = {"move_forward", "move_back", "move_left", "move_right"}

        async def _do_send():
            if kwargs:
                await mgr.send_command_with_args(name, key, **kwargs)
            else:
                await mgr.send_command(name, key)

        try:
            device = mgr.get_device_by_name(name)
            sys_status = getattr(getattr(device, "state", None), "report_data", None)
            sys_status = getattr(getattr(sys_status, "dev", None), "sys_status", None)
            self.logger.debug(f"_send_command ctx key={key} sys_status={sys_status} kwargs={kwargs}")

            await _do_send()

            if key not in nosync:
                with contextlib.suppress(Exception):
                    await mgr.start_sync(name, retry=1)
            else:
                self.logger.debug(f"Skip sync for movement key={key}")

            self._set_status(dev_id, f"Command sent: {key}")

            refresh_after = {
                "start_job": 0.6,
                "cancel_job": 0.6,
                "pause_execute_task": 0.6,
                "return_to_dock": 0.8,
                "move_forward": 0.3,
                "move_back": 0.3,
                "move_left": 0.3,
                "move_right": 0.3,
            }
            delay = refresh_after.get(key)
            if delay is not None:
                self._force_refresh_states(
                    dev_id,
                    delay=delay,
                    min_interval=(0.8 if key not in nosync else 1.5),
                )
            return

        except Exception as ex:
            # 1. If we are already doing a relogin, queue the command
            if self._cloud_relogin_in_progress.get(dev_id, False) and self._is_auth_error(ex):
                self.logger.debug(
                    f"_send_command: auth error during relogin, queuing '{key}' for dev {dev_id}: {ex}"
                )
                self._pending_commands.setdefault(dev_id, []).append((key, dict(kwargs)))
                self._set_auth(dev_id, False)
                self._set_status(dev_id, f"Queued command (auth refresh): {key}")
                return

            # 2. New auth error => try relogin once then retry
            if self._is_auth_error(ex):
                self.logger.warning(
                    f"Cloud auth expired for '{dev.name}' during '{key}'; re-login and retry once."
                )
                self._set_auth(dev_id, False)
                try:
                    await self._cloud_relogin_once(dev_id)
                    await asyncio.sleep(1.0)
                    await _do_send()
                    if key not in nosync:
                        with contextlib.suppress(Exception):
                            await mgr.start_sync(name, retry=1)
                    self._set_auth(dev_id, True)
                    self._set_status(dev_id, f"Command retried: {key}")
                    self._force_refresh_states(dev_id, delay=0.6)
                    return
                except Exception as rex:
                    self.logger.error(f"Retry failed for '{dev.name}' after re-login: {rex}")
                    self._set_status(dev_id, f"Command error: {rex}")
                    return

            # 3. Non-auth error: maintain existing behaviour
            self._set_status(dev_id, f"Command error: {ex}")
            return

    ########################################
    # State helpers
    def _set_basic(self, dev_id: int, connected: bool, status: str):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        kv = [
            {"key": "connected", "value": bool(connected)},
            {"key": "status_text", "value": status},
            {"key": "last_update", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ]
        try:
            dev.updateStatesOnServer(kv)
        except Exception:
            pass

    def _set_connected(self, dev_id: int, val: bool):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        try:
            dev.updateStateOnServer("connected", bool(val))
        except Exception:
            pass

    def _set_auth(self, dev_id: int, val: bool):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        try:
            dev.updateStateOnServer("auth_ok", bool(val))
        except Exception:
            pass
## Actions
    async def _request_quick_sync(self, dev_id: int):
        """
        Ask the mower to push status soon after a command, for snappier UI.
        """
        try:
            mgr = self._mgr.get(dev_id)
            name = self._mower_name.get(dev_id)
            if not mgr or not name:
                return
            await mgr.start_sync(name, retry=1)
        except Exception:
            pass

    # --- Add to your plugin class ---

    def areas_menu(self, filter_str: str = "", values_dict=None, type_id: str = "", target_id: int = 0) -> list:
        """
        Dynamic list for Actions.xml (deviceFilter="self"): return (hash, name).
        - Anti-flicker: falls back to cached items when live list is unavailable.
        - Never returns an empty list: uses a 'fetching' placeholder and schedules an async fetch.
        - Resolves dev_id robustly from target_id or values_dict.
        """

        def _norm_items(pairs):
            out = []
            seen = set()
            for val, label in pairs:
                try:
                    sval = str(val).strip()
                    if not sval:
                        continue
                    if sval in seen:
                        continue
                    seen.add(sval)
                    out.append((sval, str(label)))
                except Exception:
                    continue
            try:
                out.sort(key=lambda t: t[1].lower())
            except Exception:
                pass
            return out

        # 1) Resolve the device id robustly
        dev_id = 0
        try:
            if target_id:
                dev_id = int(target_id)
            elif values_dict:
                for k in ("targetId", "deviceId", "devId", "objectId"):
                    v = values_dict.get(k)
                    if v:
                        try:
                            dev_id = int(v)
                            break
                        except Exception:
                            continue
        except Exception:
            pass

        if not dev_id:
            # No context; return a stable placeholder
            return [("no_device", "No device context")]

        # 2) Try live list first
        live_items = []
        try:
            dev = indigo.devices.get(dev_id)
            mgr = self._mgr.get(dev_id)
            mower_name = self._mower_name.get(dev_id)
            if mgr and mower_name:
                mowing_device = mgr.mower(mower_name)
                if mowing_device:
                    m = getattr(mowing_device, "map", None)
                    names = getattr(m, "area_name", []) if m else []
                    area_tbl = getattr(m, "area", {}) if m else {}

                    # Primary: area_name list (hash + name)
                    for an in (names or []):
                        try:
                            ah = getattr(an, "hash", None)
                            if ah in (None, 0, "0", ""):
                                continue
                            nm = getattr(an, "name", None) or f"area {ah}"
                            live_items.append((str(int(ah)), nm))
                        except Exception:
                            continue

                    # Fallback: map.area keys
                    if not live_items and isinstance(area_tbl, dict):
                        for ah in area_tbl.keys():
                            try:
                                if ah in (None, 0, "0", ""):
                                    continue
                                live_items.append((str(int(ah)), f"area {ah}"))
                            except Exception:
                                continue
        except Exception:
            self.logger.exception("areas_menu live build failed")

        live_items = _norm_items(live_items)

        # 3) If we got live items, cache and return them
        if live_items:
            try:
                self._areas_cache[dev_id] = [{"id": v, "name": n} for (v, n) in live_items]
                self.logger.debug(f"areas_menu: returning LIVE areas ({len(live_items)}) for dev_id={dev_id}")
            except Exception:
                pass
            return live_items

        # 4) Fall back to cache (anti-flicker)
        cache = self._areas_cache.get(dev_id, []) or []
        if cache:
            cached_items = _norm_items([(c.get("id", ""), c.get("name", "")) for c in cache])
            if cached_items:
                self.logger.debug(f"areas_menu: returning CACHED areas ({len(cached_items)}) for dev_id={dev_id}")
                return cached_items

        # 5) Kick async fetch and return placeholder (non-empty id)
        try:
            if getattr(self, "_event_loop", None):
                async def _do_fetch():
                    try:
                        await self._fetch_areas(dev_id)
                    except Exception:
                        self.logger.debug("areas_menu: _fetch_areas raised", exc_info=True)

                self._event_loop.call_soon_threadsafe(asyncio.create_task, _do_fetch())
        except Exception:
            self.logger.debug("areas_menu: scheduling fetch failed", exc_info=True)

        self.logger.debug(f"areas_menu: no live/cache areas; returning placeholder for dev_id={dev_id}")
        return [("fetching", "Fetching areas...")]

    def select_area_action(self, action, dev):
        """
        Action: Select Work Area
        - Writes 'area_name' and 'zone_hash' device states
        - Stores the selection in dev.pluginProps['selected_area_hash'] for later reuse
        """
        try:
            raw = (action.props.get("areaHash") or "").strip()
            if not raw or raw == "0":
                self.logger.warning(f"Select Work Area: no area selected for '{dev.name}'")
                return

            try:
                sel_hash = int(raw)
            except Exception:
                self.logger.error(f"Select Work Area: invalid area hash '{raw}' for '{dev.name}'")
                return

            mgr = self._mgr.get(dev.id)
            mower_name = self._mower_name.get(dev.id)
            mowing_device = mgr.mower(mower_name) if mgr and mower_name else None
            if not mowing_device:
                self.logger.error(f"Select Work Area: mower not ready for '{dev.name}'")
                return

            # Resolve a friendly name (prefer map.area_name; fallback to frame indirection)
            nm = None
            try:
                m = getattr(mowing_device, "map", None)
                names = getattr(m, "area_name", []) if m else []
                for an in (names or []):
                    if getattr(an, "hash", None) == sel_hash:
                        nm = getattr(an, "name", None) or f"area {sel_hash}"
                        break
                if nm is None and m:
                    area_tbl = getattr(m, "area", {}) if m else {}
                    aentry = area_tbl.get(sel_hash) if isinstance(area_tbl, dict) else None
                    if aentry and getattr(aentry, "data", None):
                        frame0 = aentry.data[0] if len(aentry.data) > 0 else None
                        if frame0 is not None:
                            for an in (names or []):
                                if getattr(an, "hash", None) == getattr(frame0, "hash", None):
                                    nm = getattr(an, "name", None) or f"area {sel_hash}"
                                    break
                if nm is None:
                    nm = f"area {sel_hash}"
            except Exception:
                self.logger.exception("Area name resolution failed")
                nm = f"area {sel_hash}"

            # Update Indigo states exactly matching your schema
            kv = []
            if "zone_hash" in dev.states:
                kv.append({"key": "zone_hash", "value": sel_hash})
            if "area_name" in dev.states:
                kv.append({"key": "area_name", "value": str(nm)})
            if kv:
                dev.updateStatesOnServer(kv)
                self.logger.info(f"Selected area for '{dev.name}': {nm} ({sel_hash})")
            else:
                self.logger.warning(f"Device '{dev.name}' missing area_name/zone_hash states; selection not shown.")

            # Persist selection to pluginProps for later actions
            try:
                props = dev.pluginProps or indigo.Dict()
                props["selected_area_hash"] = str(sel_hash)
                dev.replacePluginPropsOnServer(props)
            except Exception:
                self.logger.exception("Failed to persist selected_area_hash in pluginProps")

        except Exception:
            self.logger.exception("select_area_action failed")

    def refresh_area_list_action(self, action, dev):
        """
        Action: Refresh Area List
        - Requests map sync and area-name list from the mower (throttled externally)
        - The area_menu list will populate as messages arrive
        """
        try:
            mgr = self._mgr.get(dev.id)
            mower_name = self._mower_name.get(dev.id)
            if not mgr or not mower_name:
                self.logger.error(f"Refresh Area List: manager or mower not ready for '{dev.name}'")
                return

            # Best-effort: kick map sync and get_area_name_list
            async def _do():
                try:
                    await mgr.start_map_sync(mower_name)
                except Exception:
                    self.logger.debug("start_map_sync failed or not available", exc_info=True)
                try:
                    # Prefer by args if available (needs iot_id)
                    device = mgr.get_device_by_name(mower_name)
                    if device:
                        await mgr.send_command_with_args("get_area_name_list", device_id=device.iot_id)
                    else:
                        await mgr.send_command(mower_name, "get_area_name_list")
                except Exception:
                    self.logger.debug("get_area_name_list send failed", exc_info=True)

            # Schedule on your loop if present
            if getattr(self, "_event_loop", None):
                self._event_loop.call_soon_threadsafe(asyncio.create_task, _do())
            else:
                # Fallback: run synchronously if your mgr supports sync (most don’t)
                self.logger.debug("Async loop not running; area refresh queued but may not execute immediately")

            self.logger.info(f"Requested area list refresh for '{dev.name}'")

        except Exception:
            self.logger.exception("refresh_area_list_action failed")
    # Replace previous CSV variant with dynamic list aware action
    def start_mowing_areas_action(self, action, dev):
        """
        Start mowing selected areas with explicit blade height:
          - areas: list of area hashes (from dynamic list)
          - bladeHeight: menu 30–100mm (default 65mm)
          - Build GenerateRouteInformation(one_hashs=..., blade_height=...)
          - Then start_job and quick sync
        """
        import re, json

        # --- Parse blade height from Action UI (default 65; clamp 30..100). Yuka/-mini will override to -10. ---
        chosen_height = 65
        try:
            raw_h = (action.props.get("bladeHeight") or "").strip()
            if raw_h:
                hh = int(raw_h)
                if hh < 30:
                    hh = 30
                if hh > 100:
                    hh = 100
                chosen_height = hh
        except Exception:
            chosen_height = 65

        # --- Normalize areas selection to list[int] hashes ---
        raw = action.props.get("areas")
        self.logger.debug(
            f"start_mowing_areas_action: raw areas={raw!r} (type={type(raw)}), bladeHeight={chosen_height}mm")
        candidates = []
        if isinstance(raw, (list, tuple)):
            candidates = [str(x) for x in raw if str(x)]
        elif hasattr(raw, "__class__") and raw.__class__.__name__ in ("List", "indigo.List"):
            candidates = [str(x) for x in raw if str(x)]
        elif isinstance(raw, str):
            s = raw.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    if isinstance(arr, list):
                        candidates = [str(x) for x in arr if str(x)]
                except Exception:
                    candidates = []
            if not candidates:
                candidates = re.findall(r"\b\d{6,20}\b", s)
        else:
            candidates = re.findall(r"\b\d{6,20}\b", str(raw))

        hashes = []
        seen = set()
        for s in candidates:
            try:
                h = int(s)
                if h not in seen:
                    seen.add(h)
                    hashes.append(h)
            except Exception:
                self.logger.warning(f"Ignoring invalid area hash '{s}' in selection for '{dev.name}'")

        # If none, start last plan
        if not hashes:
            self.logger.info(
                f"Start mowing requested for '{dev.name}' with no explicit areas (using last plan); blade {chosen_height}mm")

            async def _fallback():
                await self._send_command(dev.id, "start_job")
                await self._request_quick_sync(dev.id)

            return self._schedule(dev.id, _fallback())

        # Friendly names (best-effort)
        mgr = self._mgr.get(dev.id)
        mower_name = self._mower_name.get(dev.id)
        mowing_device = mgr.mower(mower_name) if mgr and mower_name else None
        area_names = {}
        if mowing_device and getattr(mowing_device, "map", None):
            names = getattr(mowing_device.map, "area_name", []) or []
            for h in hashes:
                nm = None
                for an in names:
                    if getattr(an, "hash", None) == h:
                        nm = getattr(an, "name", None)
                        break
                area_names[h] = nm or f"area {h}"
        pretty = ", ".join([f"{area_names.get(h, h)} ({h})" for h in hashes])

        # Decide final blade height (Yuka override)
        blade_height_to_use = chosen_height
        try:
            from pymammotion.utility.device_type import DeviceType
            if mower_name and (DeviceType.is_yuka(mower_name) or DeviceType.is_yuka_mini(mower_name)):
                blade_height_to_use = -10  # required by Yuka firmware
        except Exception:
            pass

        self.logger.info(
            f"Start mowing for '{dev.name}' with areas: {pretty} | blade height: "
            f"{blade_height_to_use if blade_height_to_use >= 0 else 'Yuka auto (-10)'}"
        )

        # Immediate UI hint for area + blade height (non-negative)
        first = hashes[0]
        kv = []
        if "zone_hash" in dev.states:
            kv.append({"key": "zone_hash", "value": first})
        if "area_name" in dev.states:
            kv.append({"key": "area_name", "value": str(area_names.get(first, f'area {first}'))})
        if blade_height_to_use >= 0 and "blade_height_mm" in dev.states:
            kv.append({"key": "blade_height_mm", "value": int(blade_height_to_use)})
        if kv:
            try:
                dev.updateStatesOnServer(kv)
            except Exception:
                pass

        # Generate route (include blade_height) then start job
        from pymammotion.data.model import GenerateRouteInformation
        async def _do():
            try:
                gri = GenerateRouteInformation(
                    one_hashs=list(hashes),
                    blade_height=int(blade_height_to_use),
                )
            except Exception:
                gri = GenerateRouteInformation(one_hashs=list(hashes))

            await self._send_command(
                dev.id,
                "generate_route_information",
                generate_route_information=gri,
            )
            await self._send_command(dev.id, "start_job")
            await self._request_quick_sync(dev.id)

        self._schedule(dev.id, _do())
    # --- HA coordinator parity actions (drop into Plugin class) ---
#
    # === Advanced Start Mowing (instrumented) ===================================
    # Drop this into plugin.py (inside your Plugin class). It includes:
    # - start_mowing_advanced_action (Indigo Action callback)
    # - _debug_before_start / _debug_after_plan helpers
    # - Progress instrumentation and route planning sequence
    # Keep other helper functions minimal; this is self-contained.

    # ---------------------------------------------------------------------------
    # Instrumentation helpers (place inside Plugin class – near other helpers)
    # ---------------------------------------------------------------------------
    async def _debug_before_start(self, dev_id: int):
        mgr = self._mgr.get(dev_id)
        name = self._mower_name.get(dev_id)
        if not mgr or not name:
            return
        try:
            md = mgr.mower(name)
            rd = getattr(md, "report_data", None)
            devs = getattr(rd, "dev", None) if rd else None
            work = getattr(rd, "work", None) if rd else None
            sys_status = getattr(devs, "sys_status", None)
            charge_state = getattr(devs, "charge_state", None)
            bp_info = getattr(work, "bp_info", None) if work else None
            area_val = getattr(work, "area", None) if work else None
            self.logger.debug(
                f"[PRE-START] sys_status={sys_status} charge_state={charge_state} "
                f"bp_info={bp_info} work.area={area_val} hex={hex(area_val) if isinstance(area_val, int) else area_val}"
            )
        except Exception:
            self.logger.exception("_debug_before_start failed")

    async def _debug_after_plan(self, dev_id: int, label: str):
        mgr = self._mgr.get(dev_id)
        name = self._mower_name.get(dev_id)
        if not mgr or not name:
            return
        try:
            md = mgr.mower(name)
            rd = getattr(md, "report_data", None)
            devs = getattr(rd, "dev", None) if rd else None
            work = getattr(rd, "work", None) if rd else None
            sys_status = getattr(devs, "sys_status", None)
            bp_info = getattr(work, "bp_info", None) if work else None
            area_val = getattr(work, "area", None) if work else None
            upper16 = (area_val >> 16) & 0xFFFF if isinstance(area_val, int) else None
            lower16 = area_val & 0xFFFF if isinstance(area_val, int) else None
            self.logger.debug(
                f"[{label}] sys_status={sys_status} bp_info={bp_info} work.area={area_val} "
                f"upper16={upper16} lower16={lower16}"
            )
        except Exception:
            self.logger.exception("_debug_after_plan failed")

    # ---------------------------------------------------------------------------
    # Indigo Action callback: Start Mowing (Advanced)
    # ---------------------------------------------------------------------------
    def _build_route_information_from_op(self, op, device_name):
        from pymammotion.data.model import GenerateRouteInformation
        from pymammotion.utility.device_type import DeviceType
        from pymammotion.data.model.device_config import create_path_order
        toward_included_angle = op.toward_included_angle if op.channel_mode == 1 else 0
        try:
            if DeviceType.is_luba1(device_name):
                # HA forces angle modes off for Luba1
                toward_mode = 0
                toward_included_angle = 0
            else:
                toward_mode = op.toward_mode
            return GenerateRouteInformation(
                one_hashs=list(op.areas),
                rain_tactics=op.rain_tactics,
                speed=op.speed,
                ultra_wave=op.ultra_wave,
                toward=op.toward,
                toward_included_angle=toward_included_angle,
                toward_mode=toward_mode,
                blade_height=op.blade_height,
                channel_mode=op.channel_mode,
                channel_width=op.channel_width,
                job_mode=op.job_mode,
                edge_mode=op.mowing_laps,
                path_order=create_path_order(op, device_name),
                obstacle_laps=op.obstacle_laps,
            )
        except Exception as ex:
            self.logger.error(f"_build_route_information_from_op failed: {ex}")
            return GenerateRouteInformation(one_hashs=list(op.areas))

    # Minimal, practical tweak: keep your current Advanced Start flow, just add a short
    # undock (if charging), a plan→start delay, then a quick sync + UI refresh.
    # No progress decoding changes, no job/job_id changes, no extra helpers.

    def start_mowing_advanced_action(self, action, dev):
        import asyncio, json, re

        # Reuse your existing parsing – keep this minimal:
        def _iget(key, default=None):
            try:
                return int((action.props.get(key) or "").strip())
            except Exception:
                return default

        def _fget(key, default=None):
            try:
                return float((action.props.get(key) or "").strip())
            except Exception:
                return default

        def _bget(key, default=False):
            v = action.props.get(key)
            if isinstance(v, bool): return v
            s = (str(v) or "").strip().lower()
            return s in ("1", "true", "yes", "on")

        # Areas (unchanged)
        raw_areas = action.props.get("areas")
        self.logger.debug(f"start_mowing_advanced_action: raw areas={raw_areas!r}")
        candidates = []
        if isinstance(raw_areas, (list, tuple)):
            candidates = [str(x) for x in raw_areas if str(x)]
        elif isinstance(raw_areas, str):
            s = raw_areas.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    if isinstance(arr, list):
                        candidates = [str(x) for x in arr if str(x)]
                except Exception:
                    candidates = []
            if not candidates:
                candidates = re.findall(r"\b\d{5,20}\b", s)
        else:
            candidates = re.findall(r"\b\d{5,20}\b", str(raw_areas or ""))

        areas = []
        seen = set()
        for s in candidates:
            try:
                h = int(s)
                if h not in seen:
                    seen.add(h)
                    areas.append(h)
            except Exception:
                self.logger.warning(f"Ignoring invalid area hash '{s}' for '{dev.name}'")

        # Settings (leave as-is; defaults kept simple)
        payload = {
            "is_mow": _bget("is_mow", True),
            "is_dump": _bget("is_dump", True),
            "is_edge": _bget("is_edge", False),
            "collect_grass_frequency": _iget("collect_grass_frequency", 10),
            "border_mode": _iget("border_mode", 1),
            "job_version": _iget("job_version", 0),
            "job_id": _iget("job_id", 0),
            "speed": _fget("speed", 0.3),
            "ultra_wave": _iget("ultra_wave", 2),
            "channel_mode": _iget("channel_mode", 0),
            "channel_width": _iget("channel_width", 25),
            "rain_tactics": _iget("rain_tactics", 1),
            "blade_height": _iget("blade_height", 65),
            "toward": _iget("toward", 0),
            "toward_included_angle": _iget("toward_included_angle", 0),
            "toward_mode": _iget("toward_mode", 0),
            "mowing_laps": _iget("mowing_laps", 1),
            "obstacle_laps": _iget("obstacle_laps", 0),
            "start_progress": _iget("start_progress", 0),
            "areas": areas,
        }

        # Friendly names for log (optional, unchanged)
        area_names = {}
        try:
            mgr = self._mgr.get(dev.id)
            mower_name = self._mower_name.get(dev.id)
            mowing_device = mgr.mower(mower_name) if mgr and mower_name else None
            if mowing_device and getattr(mowing_device, "map", None):
                names = getattr(mowing_device.map, "area_name", []) or []
                for h in payload["areas"]:
                    nm = None
                    for an in names:
                        if getattr(an, "hash", None) == h:
                            nm = getattr(an, "name", None)
                            break
                    area_names[h] = nm or f"area {h}"
        except Exception:
            pass
        pretty = ", ".join([f"{area_names.get(h, h)} ({h})" for h in payload["areas"]]) if payload["areas"] else "last plan"
        self.logger.info(
            f"Advanced start mowing for '{dev.name}': areas={pretty}; "
            f"speed={payload['speed']} channel_mode={payload['channel_mode']} "
            f"blade={payload['blade_height']}mm start_progress={payload['start_progress']}"
        )

        async def _run():
            name = self._mower_name.get(dev.id)
            mgr = self._mgr.get(dev.id)
            if not name or not mgr:
                self._set_status(dev.id, "No mower selected")
                return

            # If docked & charging, release
            try:
                from pymammotion.utility.constant.device_constant import WorkMode as _WM
            except Exception:
                try:
                    from pymammotion.utility.constant import WorkMode as _WM
                except Exception:
                    _WM = None

            try:
                md = mgr.mower(name)
                rd = getattr(md, "report_data", None)
                devs = getattr(rd, "dev", None) if rd else None
                mode_int = int(getattr(devs, "sys_status", 0)) if devs else None
                charge_state = int(getattr(devs, "charge_state", 0)) if devs else 0
            except Exception:
                mode_int = None
                charge_state = 0

            if _WM and mode_int == int(_WM.MODE_READY) and charge_state != 0:
                self.logger.info(f"'{dev.name}' is charging; sending release_from_dock")
                try:
                    await self._send_command(dev.id, "release_from_dock")
                    await asyncio.sleep(1.2)
                except Exception:
                    pass

            # Plan like HA
            planned = False
            try:
                from pymammotion.data.model.device_config import OperationSettings, create_path_order
                op = OperationSettings.from_dict(dict(payload))

                # Force is_dump False if no collector installed (HA parity)
                try:
                    device = mgr.get_device_by_name(name)
                    coll = getattr(getattr(getattr(device, "state", None), "report_data", None), "dev", None)
                    coll = getattr(getattr(coll, "collector_status", None), "collector_installation_status", None)
                    if coll == 0:
                        op.is_dump = False
                except Exception:
                    pass

                # Yuka override
                try:
                    from pymammotion.utility.device_type import DeviceType
                    if name and (DeviceType.is_yuka(name) or DeviceType.is_yuka_mini(name)):
                        op.blade_height = -10
                except Exception:
                    pass

                # Minimal path order like HA
                try:
                    op.path_order = create_path_order(op, name) or b"\x01"
                except Exception:
                    op.path_order = b"\x01"

                # Build the HA subset of GRI fields (don’t send job_mode/edge_mode/obstacle_laps/etc.)
                from pymammotion.data.model import GenerateRouteInformation
                gri = GenerateRouteInformation(
                    one_hashs=list(op.areas),
                    rain_tactics=op.rain_tactics,
                    speed=op.speed,
                    ultra_wave=op.ultra_wave,
                    toward=op.toward,
                    toward_included_angle=(op.toward_included_angle if op.channel_mode == 1 else 0),
                    toward_mode=op.toward_mode,
                    blade_height=op.blade_height,
                    channel_mode=op.channel_mode,
                    channel_width=op.channel_width,
                    path_order=op.path_order,
                )
                self.logger.debug(f"Planning route (HA subset) height={op.blade_height} speed={op.speed}")
                await self._send_command(dev.id, "generate_route_information", generate_route_information=gri)
                planned = True
            except Exception as ex:
                self.logger.error(f"Plan route failed: {ex}")

            if not planned:
                self._set_status(dev.id, "Plan failed")
                return

            # Start job (firmware will spin blades once WORKING)
            await asyncio.sleep(0.8)
            try:
                await self._send_command(dev.id, "start_job")
            except Exception as ex:
                self._set_status(dev.id, f"start_job error: {ex}")
                return

            # Optional: short wait to observe transition, then quick sync
            for _ in range(8):
                await asyncio.sleep(1.0)
                try:
                    md = mgr.mower(name)
                    rd = getattr(md, "report_data", None)
                    devs = getattr(rd, "dev", None) if rd else None
                    mode_now = int(getattr(devs, "sys_status", 0)) if devs else 0
                    if _WM and mode_now == int(_WM.MODE_WORKING):
                        self.logger.info(f"'{dev.name}' transitioned to WORKING")
                        break
                except Exception:
                    pass

            try:
                await self._request_quick_sync(dev.id)
            except Exception:
                pass
            self._force_refresh_states(dev.id, delay=0.5)

        self._schedule(dev.id, _run())

    def set_rain_detection_action(self, action, dev):
        """Enable/disable rain detection."""
        try:
            on = bool(action.props.get("onOff", False))
            self.logger.info(f"Set rain detection to {on} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_rain_detection", on_off=on))
        except Exception:
            self.logger.exception("set_rain_detection_action failed")

    def read_rain_detection_action(self, action, dev):
        """Query rain detection setting."""
        try:
            self.logger.info(f"Read rain detection for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "read_rain_detection"))
        except Exception:
            self.logger.exception("read_rain_detection_action failed")

    def set_sidelight_action(self, action, dev):
        """Set side light mode (int)."""
        try:
            raw = (action.props.get("mode") or "").strip()
            mode = int(raw)
            self.logger.info(f"Set sidelight mode={mode} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_sidelight", on_off=mode))
        except Exception:
            self.logger.exception("set_sidelight_action failed")

    def read_sidelight_action(self, action, dev):
        try:
            self.logger.info(f"Read sidelight for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "read_sidelight"))
        except Exception:
            self.logger.exception("read_sidelight_action failed")

    def set_manual_light_action(self, action, dev):
        try:
            manual = bool(action.props.get("manual", False))
            self.logger.info(f"Set manual light to {manual} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_manual_light", manual_ctrl=manual))
        except Exception:
            self.logger.exception("set_manual_light_action failed")

    def set_night_light_action(self, action, dev):
        try:
            night = bool(action.props.get("night", False))
            self.logger.info(f"Set night light to {night} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_night_light", night_light=night))
        except Exception:
            self.logger.exception("set_night_light_action failed")

    def set_traversal_mode_action(self, action, dev):
        try:
            raw = (action.props.get("mode") or "").strip()
            mode = int(raw)
            self.logger.info(f"Set traversal mode={mode} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_traversal_mode", context=mode))
        except Exception:
            self.logger.exception("set_traversal_mode_action failed")

    def set_turning_mode_action(self, action, dev):
        try:
            raw = (action.props.get("mode") or "").strip()
            mode = int(raw)
            self.logger.info(f"Set turning mode={mode} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_turning_mode", context=mode))
        except Exception:
            self.logger.exception("set_turning_mode_action failed")

    def blade_height_action(self, action, dev):
        """Set blade height (mm)."""
        try:
            raw = (action.props.get("height") or "").strip()
            height = int(raw)
            self.logger.info(f"Set blade height={height}mm for '{dev.name}'")

            # Primary
            async def _do():
                try:
                    await self._send_command(dev.id, "blade_height", height=height)
                except Exception:
                    # Fallback name used in some lib builds
                    await self._send_command(dev.id, "set_blade_height", height=height)

            self._schedule(dev.id, _do())
        except Exception:
            self.logger.exception("blade_height_action failed")

    def cutter_speed_action(self, action, dev):
        """Set cutter speed mode (int)."""
        try:
            raw = (action.props.get("mode") or "").strip()
            mode = int(raw)
            self.logger.info(f"Set cutter speed mode={mode} for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_cutter_speed", mode=mode))
        except Exception:
            self.logger.exception("cutter_speed_action failed")

    def set_speed_action(self, action, dev):
        """Set mowing speed (float m/s expected by HA)."""
        try:
            raw = (action.props.get("speed") or "").strip()
            speed = float(raw)
            self.logger.info(f"Set mowing speed={speed} m/s for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "set_speed", speed=speed))
        except Exception:
            self.logger.exception("set_speed_action failed")

    def leave_dock_action(self, action, dev):
        try:
            self.logger.info(f"Leave dock requested for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "leave_dock"))
        except Exception:
            self.logger.exception("leave_dock_action failed")

    def cancel_task_action(self, action, dev):
        try:
            self.logger.info(f"Cancel task requested for '{dev.name}'")

            async def _do():
                try:
                    await self._send_command(dev.id, "cancel_task")
                except Exception:
                    await self._send_command(dev.id, "cancel_job")

            self._schedule(dev.id, _do())
        except Exception:
            self.logger.exception("cancel_task_action failed")

    def move_forward_action(self, action, dev):
        try:
            spd = float((action.props.get("speed") or "0").strip())
            self.logger.info(f"Move forward speed={spd} for '{dev.name}'")

            async def _run():
                await self._send_command(dev.id, "move_forward", linear=spd)

            self._schedule(dev.id, _run())
        except Exception:
            self.logger.exception("move_forward_action failed")

    def move_back_action(self, action, dev):
        try:
            spd = float((action.props.get("speed") or "0").strip())
            self.logger.info(f"Move back speed={spd} for '{dev.name}'")

            async def _run():
                await self._send_command(dev.id, "move_back", linear=spd)

            self._schedule(dev.id, _run())
        except Exception:
            self.logger.exception("move_back_action failed")

    def move_left_action(self, action, dev):
        try:
            spd = float((action.props.get("speed") or "0").strip())
            self.logger.info(f"Move left speed={spd} for '{dev.name}'")

            async def _run():
                await self._send_command(dev.id, "move_left", angular=spd)

            self._schedule(dev.id, _run())
        except Exception:
            self.logger.exception("move_left_action failed")

    def move_right_action(self, action, dev):
        try:
            spd = float((action.props.get("speed") or "0").strip())
            self.logger.info(f"Move right speed={spd} for '{dev.name}'")

            async def _run():
                await self._send_command(dev.id, "move_right", angular=spd)

            self._schedule(dev.id, _run())
        except Exception:
            self.logger.exception("move_right_action failed")

    def rtk_dock_location_action(self, action, dev):
        try:
            self.logger.info(f"Set RTK dock location for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "rtk_dock_location"))
        except Exception:
            self.logger.exception("rtk_dock_location_action failed")

    def relocate_charging_station_action(self, action, dev):
        try:
            self.logger.info(f"Relocate charging station for '{dev.name}'")
            self._schedule(dev.id, self._send_command(dev.id, "relocate_charging_station"))
        except Exception:
            self.logger.exception("relocate_charging_station_action failed")
###


    # --- replace your camera_start_action with this one (uses cached userAccount) ---
    # Global cache (already added earlier)
    # self._webrtc_tokens = {}            # {'app_id':..., 'channel':..., 'token':..., 'uid':..., 'expire':...}
    # self._webrtc_active_dev_id = None   # last device that refreshed tokens

    def camera_refresh_stream_action(self, action, dev):
        mgr = self._mgr.get(dev.id);
        mower_name = self._mower_name.get(dev.id)
        if not mgr or not mower_name:
            self.logger.error(f"Camera refresh: manager/mower not ready for '{dev.name}'")
            return

        async def _do():
            try:
                device = mgr.get_device_by_name(mower_name)
                stream_resp = await device.mammotion_http.get_stream_subscription(device.iot_id)
                raw = stream_resp.data.to_dict() if getattr(stream_resp, "data", None) else {}
                # Log keys we actually got
                self.logger.debug(f"camera_refresh_stream: raw stream dict keys: {list(raw.keys())}")

                # Cache account id (HA parity)
                try:
                    http_resp = getattr(device.mammotion_http, "response", None)
                    if http_resp and getattr(http_resp, "data", None):
                        ui = getattr(http_resp.data, "userInformation", None)
                        if ui and getattr(ui, "userAccount", None) is not None:
                            self._user_account_id[dev.id] = int(ui.userAccount)
                            self.logger.debug(
                                f"camera_refresh_stream: cached userAccount={self._user_account_id[dev.id]}")
                except Exception:
                    pass

                # Normalize token bundle to expected keys
                app_id = raw.get("app_id") or raw.get("appId") or raw.get("appid") or ""
                channel = raw.get("channel") or raw.get("channelName") or raw.get("ch") or ""
                token = raw.get("token") or raw.get("accessToken") or raw.get("agoraToken") or ""
                uid = raw.get("uid") or raw.get("userId") or raw.get("uidStr") or ""
                expire = raw.get("expire") or raw.get("expire_ts") or raw.get("expireTime") or 0
                try:
                    expire = int(expire or 0)
                except Exception:
                    expire = 0

                self._webrtc_tokens = {
                    "app_id": str(app_id),
                    "channel": str(channel),
                    "token": str(token),
                    "uid": str(uid),
                    "expire": expire,
                }
                self._webrtc_active_dev_id = dev.id

                # Mirror to states (no props writes; no restarts)
                kv = []
                if "stream_app_id" in dev.states:  kv.append({"key": "stream_app_id", "value": str(app_id)})
                if "stream_channel" in dev.states: kv.append({"key": "stream_channel", "value": str(channel)})
                if "stream_token" in dev.states:   kv.append({"key": "stream_token", "value": "set" if token else ""})
                if "stream_uid" in dev.states:     kv.append({"key": "stream_uid", "value": str(uid)})
                if "stream_expire" in dev.states:  kv.append({"key": "stream_expire", "value": int(expire)})
                if "stream_status" in dev.states:  kv.append(
                    {"key": "stream_status", "value": ("OK" if token and app_id and channel else "Empty")})
                if kv:
                    dev.updateStatesOnServer(kv)

                url = f"http://{self._host_ip_for_links()}:{self._webrtc_port}/webrtc/player"
                self.logger.info(f"Stream subscription updated. Open player: {url}")
                if not (app_id and channel and token):
                    self.logger.warning(
                        "Stream tokens incomplete (app_id/channel/token missing) — start the video first, then refresh.")
            except Exception as exc:
                self.logger.error(f"Camera refresh failed: {exc}")

        self._schedule(dev.id, _do())


###
    def request_iot_sync_action(self, action, dev):
        """
        Start/stop continuous IoT reporting (mirrors HA async_request_iot_sync).
        """
        try:
            stop = bool(action.props.get("stop", False))
            self.logger.info(f"Request IoT sync ({'STOP' if stop else 'START'}) for '{dev.name}'")

            async def _do():
                try:
                    from pymammotion.proto import RptAct, RptInfoType
                    await self._send_command(
                        dev.id,
                        "request_iot_sys",
                        rpt_act=(RptAct.RPT_STOP if stop else RptAct.RPT_START),
                        rpt_info_type=[
                            RptInfoType.RIT_DEV_STA,
                            RptInfoType.RIT_DEV_LOCAL,
                            RptInfoType.RIT_WORK,
                            RptInfoType.RIT_MAINTAIN,
                            RptInfoType.RIT_BASESTATION_INFO,
                            RptInfoType.RIT_VIO,
                        ],
                        timeout=10000,
                        period=3000,
                        no_change_period=4000,
                        count=0,
                    )
                except Exception:
                    # Minimal fallback – act only
                    await self._send_command(
                        dev.id,
                        "request_iot_sys",
                        rpt_act=(1 if stop else 0)  # library may accept simple flags on older builds
                    )

            self._schedule(dev.id, _do())
        except Exception:
            self.logger.exception("request_iot_sync_action failed")
##
    def dump_all_state_action(self, action, dev):
        """
        Summarized diagnostic dump: emit compact dicts for core objects only with trimmed values.
        Easier to paste into tickets.
        """
        name = self._mower_name.get(dev.id)
        if not name:
            self.logger.warning(f"'{dev.name}': no mower selected")
            return
        try:
            mgr = Mammotion()
            mowing_device = mgr.mower(name)
            if mowing_device is None:
                self.logger.warning(f"'{dev.name}': mower object not available")
                return

            def summarize(obj, max_list=10, max_str=120):
                """Return a dict of scalar fields and short lists only."""
                out = {}
                if obj is None:
                    return out
                # Pydantic model support
                if hasattr(obj, "model_dump"):
                    raw = obj.model_dump()
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            if isinstance(v, (str, int, float, bool)) or v is None:
                                out[k] = (v[:max_str] + "…" if isinstance(v, str) and len(v) > max_str else v)
                            elif isinstance(v, (list, tuple)):
                                short = v[:max_list]
                                out[k] = short
                            elif isinstance(v, dict):
                                # one level shallow for dicts
                                shallow = {}
                                for kk, vv in list(v.items())[:max_list]:
                                    if isinstance(vv, (str, int, float, bool)) or vv is None:
                                        shallow[kk] = (
                                            vv[:max_str] + "…" if isinstance(vv, str) and len(vv) > max_str else vv)
                                out[k] = shallow
                    return out
                # Fallback: basic attrs
                attrs = [a for a in dir(obj) if not a.startswith("_")]
                for a in attrs:
                    try:
                        v = getattr(obj, a, None)
                    except Exception:
                        continue
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        out[a] = (v[:max_str] + "…" if isinstance(v, str) and len(v) > max_str else v)
                return out

            rd = getattr(mowing_device, "report_data", None)
            rd_dev = getattr(rd, "dev", None) if rd else None
            mower_state = getattr(mowing_device, "mower_state", None)
            mowing_state = getattr(mowing_device, "mowing_state", None)
            mow_info = getattr(mowing_device, "mow_info", None)

            self.logger.info(f"Summarized dump for '{dev.name}'")
            self.logger.debug(
                f"mowing_device.name: {getattr(mowing_device, 'name', '')}  online: {getattr(mowing_device, 'online', None)}")
            self.logger.debug(f"report_data.dev: {summarize(rd_dev)}")
            self.logger.debug(f"mower_state: {summarize(mower_state)}")
            self.logger.debug(f"mowing_state: {summarize(mowing_state)}")
            self.logger.debug(f"mow_info: {summarize(mow_info)}")
        except Exception as ex:
            self.logger.error(f"Summarized dump failed for '{dev.name}': {ex}")

    def _set_status(self, dev_id: int, text: str):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        try:
            dev.updateStateOnServer("status_text", text)
            dev.updateStateOnServer("last_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass