# Minimal WebRTC (Agora) HTTP server for Indigo plugins
# Endpoints:
#   POST /webrtc/start        -> refresh -> join -> fetch tokens
#   POST /webrtc/stop         -> leave and clear cache
#   GET  /webrtc/tokens.json  -> current token bundle (or 404)
#   GET  /webrtc/player       -> HTML player (add ?joystick=1 to show joystick)
#   POST /webrtc/move         -> one-shot movement  {dir: up|down|left|right, speed: float}
#   POST /webrtc/move_hold    -> start continuous move (optional)
#   POST /webrtc/move_release -> stop continuous move (optional)

try:
    import indigo
except ImportError:
    indigo = None

import asyncio


def start_webrtc_http(plugin):
    """
    Call this once after your plugin has created its asyncio loop.
    Example (in plugin.startup, after you set plugin._event_loop):
        from .webrtc_server import start_webrtc_http
        start_webrtc_http(self)
    """
    # Ensure defaults on plugin
    if not hasattr(plugin, "_webrtc_port"):
        plugin._webrtc_port = int(plugin.pluginPrefs.get("webrtcPort", 8787))
    if not hasattr(plugin, "_webrtc_http_started"):
        plugin._webrtc_http_started = False
    if not hasattr(plugin, "_webrtc_tokens"):
        plugin._webrtc_tokens = {}
    if not hasattr(plugin, "_webrtc_active_dev_id"):
        plugin._webrtc_active_dev_id = None
    if not hasattr(plugin, "_user_account_id"):
        plugin._user_account_id = {}

    if getattr(plugin, "_webrtc_http_started", False):
        return

    async def _serve():
        try:
            from aiohttp import web
            import json

            # Make this a plain function so every handler can "return _json_error(...)"
            def _json_error(msg, status=400):
                return web.json_response({"ok": False, "error": str(msg)}, status=status)


            # Movement dispatcher (optional – wire to your existing move handlers)
            def _send_move_command(dev_id: int, direction: str, speed: float, continuous: bool):
                mgr = getattr(plugin, "_mgr", {}).get(dev_id) if hasattr(plugin, "_mgr") else None
                mower_name = getattr(plugin, "_mower_name", {}).get(dev_id) if hasattr(plugin, "_mower_name") else None
                if not mgr or not mower_name:
                    plugin.logger.error(f"Move: manager/mower not ready for dev {dev_id}")
                    return

                async def _do():
                    try:
                        # Use the Indigo device for action handlers (NOT the Mammotion manager/wrapper)
                        indigo_dev = indigo.devices.get(dev_id)
                        if not indigo_dev:
                            plugin.logger.error(f"Move: Indigo device id {dev_id} not found")
                            return

                        # Build an Indigo-like action with props['speed'] as a string (as your actions expect)
                        try:
                            spd = float(speed)
                        except Exception:
                            spd = 0.0
                        from types import SimpleNamespace
                        action = SimpleNamespace(props={"speed": f"{spd}"})

                        if direction == "up" and hasattr(plugin, "move_forward_action"):
                            plugin.move_forward_action(action, indigo_dev)
                        elif direction == "down" and hasattr(plugin, "move_back_action"):
                            plugin.move_back_action(action, indigo_dev)
                        elif direction == "left" and hasattr(plugin, "move_left_action"):
                            plugin.move_left_action(action, indigo_dev)
                        elif direction == "right" and hasattr(plugin, "move_right_action"):
                            plugin.move_right_action(action, indigo_dev)
                        else:
                            plugin.logger.debug(f"Move stub: {direction} @ {spd} (continuous={continuous})")
                    except Exception as ex:
                        plugin.logger.error(f"Move command failed: {ex}")

                if getattr(plugin, "_event_loop", None):
                    plugin._event_loop.call_soon_threadsafe(asyncio.create_task, _do())

            # Token bundle
            async def tokens_json(request):
                if not plugin._webrtc_tokens:
                    return _json_error("no tokens (start first)", 404)
                return web.json_response({"ok": True, **plugin._webrtc_tokens})

            async def start_stream(request):
                # Choose first enabled/configured device for this plugin
                dev = None
                for d in indigo.devices.iter("self"):
                    if d.enabled and d.configured:
                        dev = d
                        break
                if dev is None:
                    return _json_error("no enabled/configured device found")

                mgr = getattr(plugin, "_mgr", {}).get(dev.id) if hasattr(plugin, "_mgr") else None
                mower_name = getattr(plugin, "_mower_name", {}).get(dev.id) if hasattr(plugin, "_mower_name") else None
                if not mgr or not mower_name:
                    return _json_error("manager/mower not ready (wait for Connected)")

                device = mgr.get_device_by_name(mower_name)
                if not device:
                    return _json_error("internal: device wrapper missing")

                # Ensure userAccount (identity)
                account_id = plugin._user_account_id.get(dev.id)
                if account_id is None:
                    try:
                        http_resp = getattr(device.mammotion_http, "response", None)
                        if http_resp and getattr(http_resp, "data", None):
                            ui = getattr(http_resp.data, "userInformation", None)
                            if ui and getattr(ui, "userAccount", None) is not None:
                                account_id = int(ui.userAccount)
                                plugin._user_account_id[dev.id] = account_id
                    except Exception:
                        pass
                if account_id is None:
                    return _json_error("userAccount not available yet")
                # Preflight: brief re-login to avoid 29003 on idle sessions (debounced inside plugin)
                try:
                    await plugin._cloud_relogin_once(dev.id, min_interval=5.0)
                except Exception:
                    pass

                # Pre-refresh (parity with HA; ignored result)
                try:
                    _ = await device.mammotion_http.get_stream_subscription(device.iot_id)
                except Exception:
                    pass

                # Join (with one retry on 29003)
                try:
                    from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
                    cmd = MammotionCommand(mower_name, int(account_id)).device_agora_join_channel_with_position(enter_state=1)
                    await device.cloud_client.send_cloud_command(device.iot_id, cmd)
                except Exception as ex:
                    txt = str(ex)
                    if ("29003" in txt) or ("identityId is blank" in txt):
                        try:
                            # Immediate re-login and short wait, then retry once
                            await plugin._cloud_relogin_once(dev.id, min_interval=0.0)
                            await asyncio.sleep(1.0)
                            from pymammotion.mammotion.commands.mammotion_command import MammotionCommand as _MC
                            cmd2 = _MC(mower_name, int(account_id)).device_agora_join_channel_with_position(enter_state=1)
                            await device.cloud_client.send_cloud_command(device.iot_id, cmd2)
                        except Exception as ex2:
                            return _json_error(f"join failed: {ex2}")
                    else:
                        return _json_error(f"join failed: {ex}")

                # Small delay, then fetch fresh subscription
                try:
                    await asyncio.sleep(1.2)
                except Exception:
                    pass

                try:
                    stream_resp = await device.mammotion_http.get_stream_subscription(device.iot_id)
                    raw = stream_resp.data.to_dict() if getattr(stream_resp, "data", None) else {}
                except Exception as ex:
                    return _json_error(f"token fetch failed: {ex}")

                # Normalize keys (HA: appid/channelName/token/uid)
                app_id  = raw.get("app_id") or raw.get("appId") or raw.get("appid") or ""
                channel = raw.get("channel") or raw.get("channelName") or raw.get("ch") or ""
                token   = raw.get("token") or raw.get("accessToken") or raw.get("agoraToken") or ""
                uid     = raw.get("uid") or raw.get("userId") or raw.get("uidStr") or ""
                expire  = raw.get("expire") or raw.get("expire_ts") or raw.get("expireTime") or 0
                try:
                    expire = int(expire or 0)
                except Exception:
                    expire = 0

                plugin._webrtc_tokens = {
                    "app_id": str(app_id),
                    "channel": str(channel),
                    "token": str(token),
                    "uid": str(uid),
                    "expire": expire,
                }
                plugin._webrtc_active_dev_id = dev.id

                # Mirror states (human readable; safe)
                kv = []
                if "stream_app_id" in dev.states: kv.append({"key": "stream_app_id", "value": str(app_id)})
                if "stream_channel" in dev.states: kv.append({"key": "stream_channel", "value": str(channel)})
                if "stream_token" in dev.states: kv.append({"key": "stream_token", "value": ("set" if token else "")})
                if "stream_uid" in dev.states: kv.append({"key": "stream_uid", "value": str(uid)})
                if "stream_expire" in dev.states: kv.append({"key": "stream_expire", "value": int(expire)})
                if "stream_status" in dev.states: kv.append({"key": "stream_status", "value": ("OK" if app_id and channel and token else "Empty")})
                if kv:
                    try:
                        dev.updateStatesOnServer(kv)
                    except Exception:
                        pass

                if not (app_id and channel and token):
                    return _json_error("incomplete token bundle (device not publishing?)")

                return web.json_response({"ok": True, **plugin._webrtc_tokens})

            async def stop_stream(request):
                dev_id = plugin._webrtc_active_dev_id
                if dev_id:
                    try:
                        dev = indigo.devices.get(dev_id)
                        mgr = getattr(plugin, "_mgr", {}).get(dev_id) if hasattr(plugin, "_mgr") else None
                        mower_name = getattr(plugin, "_mower_name", {}).get(dev_id) if hasattr(plugin, "_mower_name") else None
                        account_id = plugin._user_account_id.get(dev_id)
                        if mgr and mower_name and account_id and dev:
                            device = mgr.get_device_by_name(mower_name)
                            from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
                            cmd = MammotionCommand(mower_name, int(account_id)).device_agora_join_channel_with_position(enter_state=0)
                            await device.cloud_client.send_cloud_command(device.iot_id, cmd)
                    except Exception as ex:
                        plugin.logger.error(f"Stop stream leave failed: {ex}")
                plugin._webrtc_tokens = {}
                plugin._webrtc_active_dev_id = None
                return web.json_response({"ok": True})

            async def move_once(request):
                try:
                    body = await request.text()
                    data = json.loads(body or "{}")
                except Exception:
                    data = {}
                direction = (data.get("dir") or "").lower()
                try:
                    speed = float(data.get("speed") or 0.4)
                except Exception:
                    speed = 0.4
                dev_id = plugin._webrtc_active_dev_id
                if dev_id is None:
                    return _json_error("no active device")
                if direction not in ("up", "down", "left", "right"):
                    return _json_error("invalid dir")
                _send_move_command(dev_id, direction, speed, continuous=False)
                return web.json_response({"ok": True})

            async def move_hold(request):
                try:
                    data = json.loads(await request.text() or "{}")
                except Exception:
                    data = {}
                direction = (data.get("dir") or "").lower()
                try:
                    speed = float(data.get("speed") or 0.4)
                except Exception:
                    speed = 0.4
                dev_id = plugin._webrtc_active_dev_id
                if dev_id is None:
                    return _json_error("no active device")
                if direction not in ("up", "down", "left", "right"):
                    return _json_error("invalid dir")
                _send_move_command(dev_id, direction, speed, continuous=True)
                return web.json_response({"ok": True})

            async def move_release(request):
                # Implement a stop-all movement command here if your API requires it.
                return web.json_response({"ok": True})

            def _pick_dev():
                """
                Choose the active Indigo Mammotion device:
                - Prefer the device currently used for streaming (plugin._webrtc_active_dev_id)
                - Otherwise the first enabled+configured device owned by this plugin.
                Returns an indigo.Device or None.
                """
                try:
                    # Prefer active streaming device
                    active_id = getattr(plugin, "_webrtc_active_dev_id", None)
                    if active_id:
                        try:
                            d = indigo.devices.get(active_id)
                            if d and d.enabled and d.configured:
                                return d
                        except Exception:
                            pass

                    # Fallback: first enabled/configured plugin device
                    for d in indigo.devices.iter("self"):
                        if d.enabled and d.configured:
                            return d
                except Exception as ex:
                    try:
                        plugin.logger.debug(f"_pick_dev error: {ex}")
                    except Exception:
                        pass
                return None
            # Replace your existing dock handler with this one (keep it near other handlers)
            async def dock_now(request):
                from aiohttp import web
                import asyncio

                try:
                    dev = _pick_dev()
                except Exception as ex:
                    # _pick_dev not available or failed – always return JSON
                    try:
                        plugin.logger.error(f"Dock: _pick_dev error: {ex}")
                    except Exception:
                        pass
                    return web.json_response({"ok": False, "error": "internal: pick device failed"}, status=500)

                if not dev:
                    return web.json_response({"ok": False, "error": "no active device"}, status=404)

                try:
                    # Best-effort: pause or cancel any active task before dock (some firmwares require it)
                    pre_cmd_errors = []
                    for cmd in ("pause_execute_task", "cancel_job"):
                        try:
                            plugin.logger.debug(f"Dock: pre-step {cmd} for '{dev.name}'")
                            await plugin._send_command(dev.id, cmd)
                            await asyncio.sleep(0.25)
                            break
                        except Exception as ex:
                            pre_cmd_errors.append(str(ex))

                    # Send dock
                    plugin.logger.info(f"Dock: return_to_dock for '{dev.name}'")
                    await plugin._send_command(dev.id, "return_to_dock")

                    # Let normal (non-joystick) commands run sync/map state
                    return web.json_response({"ok": True})
                except Exception as ex:
                    plugin.logger.error(f"Dock command failed for '{dev.name}': {ex}")
                    # Always return JSON, never HTML, to keep the browser happy
                    return web.json_response({"ok": False, "error": str(ex)}, status=500)

            async def player(request):
                    html = '''<!doctype html>
            <html>
            <head>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width,initial-scale=1"/>
            <title>Mammotion Camera</title>
            <style>
            html,body{margin:0;padding:0;width:100%;height:100%;background:#000;color:#fff;font-family:Arial,Helvetica,sans-serif;overflow:hidden}
            #root{position:relative;width:100%;height:100%;}
            #videoStage{position:absolute;top:0;left:0;width:100%;height:100%;background:#000}
            .remoteVideo{position:absolute;top:0;left:0;width:100%;height:100%;object-fit:contain;background:#000}
            #topbar{position:fixed;top:8px;left:8px;display:flex;gap:6px;z-index:30;flex-wrap:wrap}
            button{background:#222;color:#fff;border:1px solid #444;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:13px}
            button:disabled{opacity:.4;cursor:not-allowed}
            #status{position:fixed;top:56px;left:8px;background:rgba(0,0,0,.55);padding:8px 10px;border-radius:6px;font-size:12px;line-height:1.4;z-index:30;max-width:360px}
            #controlsRow{display:flex;align-items:center;gap:8px}
            #speedWrap{display:flex;align-items:center;gap:4px}
            #speedRange{width:120px}
            #joystick{position:absolute;inset:0;pointer-events:none;z-index:20;font-size:0;display:none}
            #joystick.visible{display:block}
            .jbtn{pointer-events:auto;position:absolute;width:58px;height:58px;background:rgba(0,0,0,0.55);border:1px solid #666;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#eee;font-size:20px;font-weight:bold;user-select:none;transition:background .15s}
            .jbtn:active{background:#444}
            .j-up{top:22px;left:50%;transform:translateX(-50%)}
            .j-down{bottom:22px;left:50%;transform:translateX(-50%)}
            .j-left{left:22px;top:50%;transform:translateY(-50%)}
            .j-right{right:22px;top:50%;transform:translateY(-50%)}
            #fullscreenHint{position:fixed;bottom:8px;right:8px;font-size:11px;color:#aaa;z-index:30}
            #disclaimer{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;flex-direction:column;align-items:center;justify-content:center;color:#fff;z-index:40;padding:20px;text-align:center}
            #disclaimer p{max-width:480px;font-size:14px;line-height:1.5;margin:0 0 12px}
            #disclaimer button{margin-top:10px}
            #switchCameraBtn{display:none}
            </style>
            </head>
            <body>
            <div id="root">
              <div id="videoStage"></div>
              <div id="topbar">
                <button id="playBtn">Play</button>
                <button id="stopBtn" disabled>Stop</button>
                 <button id="dockBtn">Dock</button>   <!-- NEW -->
                <button id="switchCameraBtn">Switch</button>

                <button id="joyToggle">Joystick</button>
                <button id="reloadBtn">Reload</button>
                <div id="speedWrap">
                  <label for="speedRange" style="font-size:11px;">Speed</label>
                  <input id="speedRange" type="range" min="0.1" max="1.0" step="0.1" value="0.4"/>
                  <span id="speedVal" style="font-size:11px;">0.4 m/s</span>
                </div>
              </div>
              <div id="status">Idle.</div>
              <div id="joystick">
                <div class="jbtn j-up"   data-dir="up">▲</div>
                <div class="jbtn j-down" data-dir="down">▼</div>
                <div class="jbtn j-left" data-dir="left">◄</div>
                <div class="jbtn j-right"data-dir="right">►</div>
              </div>
              <div id="fullscreenHint">Press F for fullscreen</div>
              <div id="disclaimer">
                <h3 style="margin-top:0;">Physical Movement Warning</h3>
                <p>Using the joystick will physically move the mower. The video stream may not be perfectly real-time. Ensure the area is clear and safe before continuing.</p>
                <p>Hold a direction button for continuous movement (auto-stops after 10s or connection loss).</p>
                <button id="acceptDisclaimer">I Understand, Proceed</button>
              </div>
            </div>
            <script>
            let client=null;
            let remoteUsers=[];          // Array of Agora remote user objects with videoTrack
            let currentIndex=0;          // Index of current displayed user
            let preferredUid=localStorage.getItem('preferredMammotionCameraUid');
            let movementSpeed=parseFloat(localStorage.getItem('preferredJoystickSpeed')||'0.4');
            const dockBtn=document.getElementById('dockBtn');   // NEW
            const statusEl=document.getElementById('status');
            const playBtn=document.getElementById('playBtn');
            const stopBtn=document.getElementById('stopBtn');
            const switchBtn=document.getElementById('switchCameraBtn');
           // const fsBtn=document.getElementById('fsBtn');
            const reloadBtn=document.getElementById('reloadBtn');
            const joyToggle=document.getElementById('joyToggle');
            const joy=document.getElementById('joystick');
            const videoStage=document.getElementById('videoStage');
            const disclaimer=document.getElementById('disclaimer');
            const acceptDisclaimer=document.getElementById('acceptDisclaimer');
            const speedRange=document.getElementById('speedRange');
            const speedVal=document.getElementById('speedVal');

            speedRange.value=movementSpeed.toFixed(1);
            speedVal.textContent=movementSpeed.toFixed(1)+' m/s';

            function setStatus(msg){statusEl.innerHTML=msg;}

            function updateSpeed(){
              movementSpeed=parseFloat(speedRange.value);
              speedVal.textContent=movementSpeed.toFixed(1)+' m/s';
              localStorage.setItem('preferredJoystickSpeed', movementSpeed.toFixed(1));
            }
            speedRange.addEventListener('input', updateSpeed);

            function ensureDisclaimer(firstMoveCallback){
              if(localStorage.getItem('mammotionDisclaimerAccepted')==='yes'){
                firstMoveCallback(); return;
              }
              disclaimer.style.display='flex';
              acceptDisclaimer.onclick=()=>{
                disclaimer.style.display='none';
                localStorage.setItem('mammotionDisclaimerAccepted','yes');
                setTimeout(firstMoveCallback,150);
              };
            }
          async function sendDock(){
            try{
              dockBtn.disabled = true;
              setStatus('Sending dock command…');
              const r = await fetch('/webrtc/dock', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: '{}'  // keep consistent JSON shape, even if server ignores body
              });
        
              let ok = false, err = null;
              const ct = r.headers.get('content-type') || '';
              if (ct.includes('application/json')) {
                const j = await r.json();
                ok = j && j.ok === true;
                err = j && j.error;
              } else {
                // Server returned HTML (e.g., framework 500 page) – surface a short excerpt
                const txt = await r.text();
                err = `HTTP ${r.status} ${r.statusText}${txt ? `: ${txt.substring(0,200)}` : ''}`;
              }
        
              if (!ok) throw new Error(err || `HTTP ${r.status}`);
        
              setStatus('Docking requested.');
            } catch(e){
              setStatus('Dock error: ' + (e && e.message ? e.message : String(e)));
            } finally {
              setTimeout(()=>{ dockBtn.disabled = false; }, 2000);
            }
          }

            async function startAll(){
              if(client){setStatus('Already running');return;}
              playBtn.disabled=true; setStatus('Starting (refresh + join + tokens)...');
              try{
                const resp=await fetch('/webrtc/start',{method:'POST'});
                const data=await resp.json();
                if(!data.ok) throw new Error(data.error||'start failed');
                setStatus('Tokens received. Loading SDK...');
                await loadSdk();
                await joinAgora(data);
              }catch(e){
                setStatus('Start error: '+e.message);
                playBtn.disabled=false;
              }
            }

            async function loadSdk(){
              return new Promise((resolve,reject)=>{
                if(window.AgoraRTC){resolve();return;}
                const s=document.createElement('script');
                s.src='https://download.agora.io/sdk/release/AgoraRTC_N.js';
                s.onload=()=>resolve();
                s.onerror=()=>reject(new Error('SDK load failed'));
                document.head.appendChild(s);
              });
            }

            function clearVideoStage(){
              while(videoStage.firstChild) videoStage.removeChild(videoStage.firstChild);
            }

            function showCurrentVideo(){
              clearVideoStage();
              if(remoteUsers.length===0){
                return; // Waiting
              }
              if(currentIndex>=remoteUsers.length) currentIndex=0;
              const user=remoteUsers[currentIndex];
              const container=document.createElement('div');
              container.className='remoteContainer';
              container.style.position='absolute';
              container.style.inset='0';
              const vidDiv=document.createElement('div');
              vidDiv.className='remoteVideo';
              container.appendChild(vidDiv);
              // Label
              const label=document.createElement('div');
              label.textContent='Camera '+user.uid;
              label.style.position='absolute';
              label.style.bottom='10px';
              label.style.right='10px';
              label.style.background='rgba(0,0,0,0.55)';
              label.style.padding='4px 8px';
              label.style.fontSize='12px';
              label.style.borderRadius='4px';
              label.style.color='#fff';
              container.appendChild(label);
              videoStage.appendChild(container);
              if(user.videoTrack){
                user.videoTrack.play(vidDiv);
              }
              if(preferredUid && user.uid.toString()===preferredUid){
                // Already showing preferred
              }
            }

            function updateSwitchButton(){
              if(remoteUsers.length>1){
                switchBtn.style.display='inline-block';
              }else{
                switchBtn.style.display='none';
              }
            }

            switchBtn.addEventListener('click',()=>{
              if(remoteUsers.length===0) return;
              currentIndex=(currentIndex+1)%remoteUsers.length;
              const user=remoteUsers[currentIndex];
              localStorage.setItem('preferredMammotionCameraUid', user.uid.toString());
              preferredUid=user.uid.toString();
              showCurrentVideo();
              setStatus('Switched to camera UID='+user.uid);
            });
            dockBtn.addEventListener('click', sendDock);  // NEW
            
            async function joinAgora(t){
              if(!window.AgoraRTC) throw new Error('SDK missing');
              if(AgoraRTC.setLogLevel) AgoraRTC.setLogLevel(4);
              if(AgoraRTC.disableLogUpload) AgoraRTC.disableLogUpload();
              client=AgoraRTC.createClient({mode:'live',codec:'vp8'});
              if(client.setClientRole) await client.setClientRole('host');

              client.on('connection-state-change',(cur, prev, reason)=>{
                if(cur==='DISCONNECTED'){
                  setStatus('Connection lost: '+reason+' (Press Play to reconnect)');
                  stopBtn.disabled=true;
                  playBtn.disabled=false;
                  stopContinuousAll();
                }else if(cur==='CONNECTING'){
                  setStatus('Connecting...');
                }
              });

              client.on('user-published', async(user, mediaType)=>{
                try{
                  await client.subscribe(user, mediaType);
                  if(mediaType==='video' && user.videoTrack){
                    // Add / update remoteUsers
                    if(!remoteUsers.some(u=>u.uid===user.uid)){
                      remoteUsers.push(user);
                      // If preferred camera, set index
                      if(preferredUid && user.uid.toString()===preferredUid){
                        currentIndex=remoteUsers.length-1;
                      }
                      updateSwitchButton();
                    }
                    showCurrentVideo();
                    setStatus('Video subscribed uid='+user.uid);
                  }
                  if(mediaType==='audio' && user.audioTrack){
                    user.audioTrack.play();
                  }
                }catch(e){
                  setStatus('Subscribe failed: '+e);
                }
              });

              client.on('user-unpublished',(user, mediaType)=>{
                if(mediaType==='video'){
                  remoteUsers=remoteUsers.filter(u=>u.uid!==user.uid);
                  updateSwitchButton();
                  if(remoteUsers.length===0){
                    clearVideoStage();
                    setStatus('Video stream ended (no publishers).');
                  }else{
                    showCurrentVideo();
                  }
                }
              });

              // Join
              await client.join(t.app_id, t.channel, t.token||null, t.uid?parseInt(t.uid):null);
              setStatus('Joined channel. Awaiting video…');

              stopBtn.disabled=false;

              // Timeout if no publisher appears
              setTimeout(()=>{
                if(remoteUsers.length===0 && client){
                  setStatus('No publisher after 15s. Press Stop then Play again if mower camera should be active.');
                }
              },15000);
            }

            async function stopAll(){
              stopBtn.disabled=true;
              try{
                if(client){
                  await client.leave();
                  client=null;
                }
                remoteUsers=[];
                currentIndex=0;
                preferredUid=localStorage.getItem('preferredMammotionCameraUid');
                clearVideoStage();
                await fetch('/webrtc/stop',{method:'POST'});
                setStatus('Stopped.');
                playBtn.disabled=false;
                stopContinuousAll();
              }catch(e){
                setStatus('Stop error: '+e.message);
                playBtn.disabled=false;
              }
            }

            function toggleFullscreen(){
              const el=document.documentElement;
              if(!document.fullscreenElement){
                el.requestFullscreen().catch(()=>{});
              }else{
                document.exitFullscreen().catch(()=>{});
              }
            }
            document.addEventListener('keydown',e=>{
              if(e.key==='f'||e.key==='F'){toggleFullscreen();}
            });

            function toggleJoystick(){
              if(joy.classList.contains('visible')){
                joy.classList.remove('visible');
                joyToggle.textContent='Joystick';
                stopContinuousAll();
              }else{
                joy.classList.add('visible');
                joyToggle.textContent='Hide Joy';
              }
            }

            let continuousMap={}; // dir -> interval id

            function stopContinuousAll(){
              Object.keys(continuousMap).forEach(dir=>{
                clearInterval(continuousMap[dir]);
                delete continuousMap[dir];
              });
              fetch('/webrtc/move_release',{method:'POST'});
            }

            async function sendMove(dir, speed){
              try{
                await fetch('/webrtc/move',{
                  method:'POST',
                  headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({dir:dir,speed:speed})
                });
              }catch(e){}
            }

            function startContinuous(dir, speed){
              if(continuousMap[dir]) return;
              fetch('/webrtc/move_hold',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({dir:dir,speed:speed})
              });
              continuousMap[dir]=setInterval(()=>sendMove(dir,speed),500);
              // Safety auto-stop after 10s
              setTimeout(()=>endContinuous(dir),10000);
            }

            function endContinuous(dir){
              if(continuousMap[dir]){
                clearInterval(continuousMap[dir]);
                delete continuousMap[dir];
                fetch('/webrtc/move_release',{method:'POST'});
              }
            }

            // Joystick mouse events
            joy.addEventListener('mousedown', e=>{
              const t=e.target.closest('.jbtn'); if(!t) return;
              const dir=t.dataset.dir;
              ensureDisclaimer(()=>startContinuous(dir, movementSpeed));
            });
            joy.addEventListener('mouseup', e=>{
              const t=e.target.closest('.jbtn'); if(!t) return;
              endContinuous(t.dataset.dir);
            });
            joy.addEventListener('mouseleave', e=>{
              stopContinuousAll();
            });

            // Touch events
            joy.addEventListener('touchstart', e=>{
              const t=e.target.closest('.jbtn'); if(!t) return;
              e.preventDefault();
              ensureDisclaimer(()=>startContinuous(t.dataset.dir, movementSpeed));
            },{passive:false});
            joy.addEventListener('touchend', e=>{
              const t=e.target.closest('.jbtn'); if(!t) return;
              e.preventDefault();
              endContinuous(t.dataset.dir);
            },{passive:false});

            // Buttons
            playBtn.addEventListener('click', startAll);
            stopBtn.addEventListener('click', stopAll);
            reloadBtn.addEventListener('click', ()=>location.reload());
            //fsBtn.addEventListener('click', toggleFullscreen);
            joyToggle.addEventListener('click', toggleJoystick);
            
                        // --- Auto-start (5s delayed) unless disabled via ?auto=0 ---
            (function autoStartMaybe(){
              try{
                const qp=new URLSearchParams(location.search);
                const autoParam = qp.get('auto');
                if(autoParam === '0'){
                  setStatus('Auto-start disabled (auto=0).');
                  return;
                }
                // Delay 5s to allow plugin session and tokens to become available
                setTimeout(()=>{
                  if(!client){
                    setStatus('Auto-starting stream…');
                    startAll();
                  }
                }, 2000);
              }catch(e){
                // Silent fail; user can click Play manually
              }
            })();
            
            // Auto-show joystick if ?joystick=1
            (function initJoy(){
              const qp=new URLSearchParams(location.search);
              if(qp.get('joystick')==='1'){
                joy.classList.add('visible');
                joyToggle.textContent='Hide Joy';
              }
            })();

            // Connection safety check similar to HA's interval
            setInterval(()=>{
              if(client){
                const state=client.connectionState;
                if(state!=='CONNECTED'){
                  // Stop movements for safety
                  stopContinuousAll();
                }
              }
            }, 2500);
            </script>
            </body>
            </html>'''
                    from aiohttp import web
                    return web.Response(text=html, content_type="text/html")

            app = web.Application()
            app.router.add_post("/webrtc/start", start_stream)
            app.router.add_post("/webrtc/stop", stop_stream)
            app.router.add_get("/webrtc/tokens.json", tokens_json)
            app.router.add_get("/webrtc/player", player)
            app.router.add_post("/webrtc/move", move_once)
            app.router.add_post("/webrtc/move_hold", move_hold)
            app.router.add_post("/webrtc/move_release", move_release)
            app.router.add_post("/webrtc/dock", dock_now)  # NEW

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(plugin._webrtc_port))
            await site.start()
            plugin.logger.info(f"WebRTC server listening on port {plugin._webrtc_port}")
        except Exception as exc:
            # Log any server startup/handler exceptions
            try:
                plugin.logger.exception(exc)
            except Exception:
                pass

    if not getattr(plugin, "_event_loop", None):
        plugin.logger.error("Async loop not running; cannot start WebRTC server")
        return

    plugin._webrtc_http_started = True
    plugin._event_loop.call_soon_threadsafe(asyncio.create_task, _serve())