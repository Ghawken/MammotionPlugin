import asyncio
import json
from typing import Any, Dict

import indigo
from aiohttp import web


def setup_map_routes(app: web.Application, plugin: "Plugin") -> None:
    """
    Register Leaflet map + GeoJSON endpoints on the shared aiohttp app.

    Routes:
      - GET /map/{dev_id}         -> HTML page with Leaflet viewer
      - GET /map/{dev_id}/geojson -> GeoJSON for mower map (areas/paths/obstacles)
      - GET /map/{dev_id}/mowpath -> GeoJSON for current/last mowing path (if available)
    """

    async def _json_error(msg: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": msg}, status=status)

    async def _get_device_and_mgr(dev_id: int):
        dev = indigo.devices.get(dev_id)
        mgr = plugin._mgr.get(dev_id)
        if not dev or not mgr:
            return None, None, None
        mower_name = plugin._mower_name.get(dev_id)
        if not mower_name:
            return dev, mgr, None
        device = mgr.get_device_by_name(mower_name)
        return dev, mgr, device

    async def map_page(request: web.Request) -> web.Response:
        try:
            dev_id = int(request.match_info["dev_id"])
        except Exception:
            return await _json_error("invalid dev_id", 400)

        dev = indigo.devices.get(dev_id)
        if not dev:
            return await _json_error("device not found", 404)

        # Simple Leaflet page that calls our GeoJSON endpoints
        html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Mammotion Map – {dev.name}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  />
  <style>
    html,body {{ margin:0; padding:0; height:100%; }}
    #map {{ width:100%; height:100%; }}
    .leaflet-container {{ background:#111; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIJ3JWEfBvh0VYVxu0s1t7nuMd1gMVM="
    crossorigin=""
  ></script>
  <script>
    const devId = {dev_id};

    const map = L.map('map', {{
      center: [0, 0],
      zoom: 18,
      zoomControl: true,
    }});

    // Dark base layer (optional / no tiles)
    L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 20,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    function styleFromProps(props) {{
      const base = {{
        color: props.color || '#00ff00',
        weight: props.weight || 2,
        opacity: props.opacity ?? 0.9,
        fillOpacity: props.fillOpacity ?? 0.2
      }};
      if (props.type_name === 'mow_path') {{
        base.color = props.color || '#ffcc00';
        base.weight = props.weight || 2;
      }}
      return base;
    }}

    function addGeoJson(url, fit = false) {{
      return fetch(url)
        .then(r => r.json())
        .then(data => {{
          if (!data || data.ok === false) {{
            console.warn('GeoJSON error', data);
            return null;
          }}
          const layer = L.geoJSON(data, {{
            style: feature => styleFromProps(feature.properties || {{}}),
            pointToLayer: (feature, latlng) => {{
              const props = feature.properties || {{}};
              // Simple circle for RTK/Dock; could use Leaflet markers + icons if desired
              return L.circleMarker(latlng, styleFromProps(props));
            }}
          }}).addTo(map);
          if (fit && layer && layer.getBounds && layer.getBounds().isValid()) {{
            map.fitBounds(layer.getBounds().pad(0.1));
          }}
          return layer;
        }})
        .catch(err => console.error('GeoJSON fetch failed', err));
    }}

    // Load base map and mowing path; fit to base map
    Promise.all([
      addGeoJson(`/map/${{devId}}/geojson`, true),
      addGeoJson(`/map/${{devId}}/mowpath`, false),
    ]);
  </script>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html; charset=utf-8")

    async def map_geojson(request: web.Request) -> web.Response:
        """Full map (areas / paths / obstacles + RTK/dock)."""
        try:
            dev_id = int(request.match_info["dev_id"])
        except Exception:
            return await _json_error("invalid dev_id", 400)

        dev, mgr, device = await _get_device_and_mgr(dev_id)
        if not dev or not mgr or not device:
            return await _json_error("device not ready", 404)

        try:
            # Get RTK + dock from device.location (MowingDevice)
            mowing_device = mgr.mower(device.name)
            location = getattr(mowing_device, "location", None)
            rtk = getattr(location, "RTK", None)
            dock = getattr(location, "dock", None)

            if not (rtk and dock):
                return await _json_error("RTK/dock data not available yet", 503)

            from pymammotion.data.mower_state_manager import MowerStateManager

            state_mgr = getattr(device, "state_manager", None)
            if not isinstance(state_mgr, MowerStateManager):
                # Fallback: build a state manager view from device.state if needed
                state_mgr = MowerStateManager(device)

            geo = state_mgr.generate_geojson(rtk, dock)
            return web.json_response(geo)
        except Exception as ex:
            plugin.logger.debug(f"map_geojson failed for dev_id={dev_id}: {ex}")
            return await _json_error(str(ex), 500)

    async def map_mowpath(request: web.Request) -> web.Response:
        """Mowing path GeoJSON (if current_mow_path is present/complete)."""
        try:
            dev_id = int(request.match_info["dev_id"])
        except Exception:
            return await _json_error("invalid dev_id", 400)

        dev, mgr, device = await _get_device_and_mgr(dev_id)
        if not dev or not mgr or not device:
            return await _json_error("device not ready", 404)

        try:
            mowing_device = mgr.mower(device.name)
            location = getattr(mowing_device, "location", None)
            rtk = getattr(location, "RTK", None)
            if not rtk:
                return await _json_error("RTK data not available yet", 503)

            from pymammotion.data.mower_state_manager import MowerStateManager

            state_mgr = getattr(device, "state_manager", None)
            if not isinstance(state_mgr, MowerStateManager):
                state_mgr = MowerStateManager(device)

            geo = state_mgr.generate_mowing_geojson(rtk)
            return web.json_response(geo)
        except Exception as ex:
            plugin.logger.debug(f"map_mowpath failed for dev_id={dev_id}: {ex}")
            return await _json_error(str(ex), 500)

    app.router.add_get("/map/{dev_id}", map_page)
    app.router.add_get("/map/{dev_id}/geojson", map_geojson)
    app.router.add_get("/map/{dev_id}/mowpath", map_mowpath)