"""Streamlit dashboard for the Ariston Lydos Hybrid water heater.

  • Live device state (auto-refreshing).
  • Manual control: target temperature, operation mode, power.
  • Rule-based controller you can edit, enable/disable, and inspect.

Run with:
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

from ariston_client import AristonClient, DeviceSnapshot
from controller import EventLog, PollerThread, RuleThread
from rules import Rule, RuleSet, load_rules, save_rules

load_dotenv()

st.set_page_config(page_title="Ariston controller", page_icon="🔥", layout="wide")


# ---------- session state -----------------------------------------------------

def _get_state(key, factory):
    if key not in st.session_state:
        st.session_state[key] = factory()
    return st.session_state[key]


log: EventLog = _get_state("log", EventLog)
ruleset: RuleSet = _get_state("ruleset", load_rules)


def _ruleset_provider() -> RuleSet:
    return st.session_state.get("ruleset", ruleset)


# We instantiate the client lazily so we don't connect on every rerun.
if "client" not in st.session_state:
    st.session_state.client = AristonClient()
client: AristonClient = st.session_state.client


# ---------- sidebar: credentials ---------------------------------------------

st.sidebar.header("Connection")

default_user = os.getenv("ARISTON_USERNAME") or os.getenv("USERNAME") or ""
default_pass = os.getenv("ARISTON_PASSWORD") or os.getenv("PASSWORD") or ""
default_gw = (os.getenv("ARISTON_GATEWAY")
              or os.getenv("MACADRESS")
              or os.getenv("SERIAL_NUMBER")
              or "")

username = st.sidebar.text_input("Username", value=default_user)
password = st.sidebar.text_input("Password", value=default_pass, type="password")
gateway = st.sidebar.text_input("Gateway (MAC or empty for first device)",
                                value=default_gw)

connect_col, disconnect_col = st.sidebar.columns(2)
if connect_col.button("Connect", use_container_width=True):
    try:
        ok = client.connect(username.strip(), password, gateway.strip() or None)
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error(f"Connect failed: {exc!r}")
        st.sidebar.error(str(exc))
    if ok:
        log.info(f"Connected to device {client._device.name!r} ({client._device.gateway}).")
        st.sidebar.success("Connected.")
    elif client.discovered:
        gws = ", ".join(d.get("gw", "?") for d in client.discovered)
        st.sidebar.warning(f"Logged in but gateway not matched. Discovered: {gws}")

st.sidebar.caption(
    "Tip: leave gateway empty to bind to the first device on your account."
)

# ---------- sidebar: scheduler -----------------------------------------------

st.sidebar.header("Schedulers")
poll_interval = st.sidebar.number_input(
    "Poll every (seconds)", min_value=60, max_value=3600, step=30, value=300,
    help="Ariston throttles aggressive callers; 300 s is the community-recommended floor.",
)
rule_interval = st.sidebar.number_input(
    "Apply rules every (seconds)", min_value=60, max_value=3600, step=30, value=300,
)
ui_refresh = st.sidebar.number_input(
    "UI auto-refresh (seconds)", min_value=2, max_value=120, step=1, value=5,
)


def _ensure_threads() -> tuple[Optional[PollerThread], Optional[RuleThread]]:
    if not client.connected:
        return None, None
    poller = st.session_state.get("poller")
    if poller is None or not poller.is_alive():
        poller = PollerThread(client, log, float(poll_interval))
        poller.start()
        st.session_state.poller = poller
    else:
        poller.set_interval(float(poll_interval))

    ruler = st.session_state.get("ruler")
    if ruler is None or not ruler.is_alive():
        ruler = RuleThread(client, poller, _ruleset_provider, log,
                           float(rule_interval))
        ruler.start()
        st.session_state.ruler = ruler
    else:
        ruler.set_interval(float(rule_interval))
    return poller, ruler


poller, ruler = _ensure_threads()

if ruler is not None:
    if ruler.enabled:
        if st.sidebar.button("⏸ Pause rule controller", use_container_width=True):
            ruler.pause()
    else:
        if st.sidebar.button("▶ Resume rule controller", use_container_width=True):
            ruler.resume()

# Auto-refresh the UI itself.
st_autorefresh(interval=int(ui_refresh) * 1000, key="ui-refresh")

# ---------- main: status ------------------------------------------------------

st.title("🔥 Ariston Lydos Hybrid controller")

if not client.connected:
    st.warning(
        "**Not connected.** Live data and manual controls are disabled. "
        "Fill the sidebar and click **Connect** when you have credentials — "
        "you can still preview and edit the rules below.",
        icon="⚠️",
    )

snapshot: Optional[DeviceSnapshot] = poller.last_snapshot if poller else None
last_at = poller.last_at if poller else None

device_label = snapshot.name if snapshot else (
    client._device.name if client.connected else "Device (not connected)"
)

top = st.columns([2, 1, 1, 1])
top[0].subheader(device_label)
top[1].metric("Current",
              f"{snapshot.current_temperature:.1f} °C" if snapshot and snapshot.current_temperature is not None else "—")
top[2].metric("Target",
              f"{snapshot.target_temperature:.0f} °C" if snapshot and snapshot.target_temperature is not None else "—")
heating = "Heating" if snapshot and snapshot.is_heating else ("Idle" if snapshot else "—")
top[3].metric("State", heating)

st.caption(
    f"Last poll: {last_at.strftime('%H:%M:%S') if last_at else '—'} · "
    f"Mode: {snapshot.mode_text if snapshot else '—'} · "
    f"Power: {'on' if snapshot and snapshot.is_on else ('off' if snapshot else '—')} · "
    f"Range: {snapshot.min_temp if snapshot else '—'}–{snapshot.max_temp if snapshot else '—'} °C"
)

if st.button("Refresh now", disabled=not client.connected):
    try:
        client.refresh()
    except Exception as exc:  # noqa: BLE001
        st.error(str(exc))

# ---------- main: rule controller --------------------------------------------

active_rule = ruleset.active_rule()
rule_box = st.container(border=True)
with rule_box:
    if active_rule is None:
        st.markdown("**Active rule:** none for this time-of-day.")
    elif snapshot and snapshot.current_temperature is not None:
        proposed = active_rule.compute_target(snapshot.current_temperature)
        st.markdown(
            f"**Active rule:** `{active_rule.name}` "
            f"({active_rule.start} – {active_rule.end}) → "
            f"target **{proposed:.0f} °C** "
            f"(current {snapshot.current_temperature:.1f} °C "
            f"+ {active_rule.offset:+g}, capped at {active_rule.cap:.0f})"
        )
    else:
        demo_cur = st.number_input(
            "Simulated current °C (no live data — preview only)",
            min_value=10.0, max_value=70.0, value=45.0, step=1.0,
        )
        proposed = active_rule.compute_target(demo_cur)
        st.markdown(
            f"**Active rule:** `{active_rule.name}` "
            f"({active_rule.start} – {active_rule.end}) → "
            f"target **{proposed:.0f} °C** "
            f"(simulated current {demo_cur:.1f} °C "
            f"+ {active_rule.offset:+g}, capped at {active_rule.cap:.0f})"
        )

# ---------- main: manual override --------------------------------------------

with st.expander("Manual override", expanded=False):
    if not client.connected:
        st.caption("Connect to enable manual controls.")
    mc1, mc2, mc3 = st.columns(3)
    disabled = not client.connected
    min_t = int(snapshot.min_temp or 40) if snapshot else 40
    max_t = int(snapshot.max_temp or 65) if snapshot else 65
    cur = int(snapshot.target_temperature or min_t) if snapshot else min_t
    new_target = mc1.slider("Target °C", min_value=min_t, max_value=max_t,
                            value=cur, disabled=disabled)
    if mc1.button("Apply target", disabled=disabled):
        try:
            client.set_target_temperature(new_target)
            log.info(f"Manual: target set to {new_target} °C.")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    modes = client.available_modes() or ["IMEMORY", "GREEN", "PROGRAM", "BOOST"]
    mode = mc2.selectbox("Operation mode", modes,
                         index=modes.index(snapshot.mode_text)
                         if snapshot and snapshot.mode_text in modes else 0,
                         disabled=disabled)
    if mc2.button("Apply mode", disabled=disabled):
        try:
            client.set_operation_mode(mode)
            log.info(f"Manual: mode set to {mode}.")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    power_on = bool(snapshot.is_on) if snapshot else False
    new_power = mc3.toggle("Power on", value=power_on, disabled=disabled)
    if mc3.button("Apply power", disabled=disabled):
        try:
            client.set_power(new_power)
            log.info(f"Manual: power → {'on' if new_power else 'off'}.")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

# ---------- main: rule editor ------------------------------------------------

st.subheader("Time-of-day rules")
st.caption(
    "Each rule covers a daily time window and computes "
    "`target = clamp(current_temp + offset, floor, cap)`. "
    "The first matching enabled rule wins. "
    "Tip: keep `cap ≤ 50` to stay on the heat-pump and avoid the resistor."
)

edited_rows = []
for idx, rule in enumerate(ruleset.rules):
    cols = st.columns([2, 1, 1, 1, 1, 1, 0.6, 0.6])
    name = cols[0].text_input("Name", value=rule.name, key=f"n{idx}")
    start = cols[1].text_input("Start", value=rule.start, key=f"s{idx}")
    end = cols[2].text_input("End", value=rule.end, key=f"e{idx}")
    offset = cols[3].number_input("Offset °C", value=float(rule.offset),
                                  step=1.0, key=f"o{idx}")
    floor = cols[4].number_input("Floor °C", value=float(rule.floor),
                                 step=1.0, key=f"f{idx}")
    cap = cols[5].number_input("Cap °C", value=float(rule.cap),
                               step=1.0, key=f"c{idx}")
    enabled = cols[6].checkbox("On", value=rule.enabled, key=f"en{idx}")
    delete = cols[7].checkbox("✕", value=False, key=f"d{idx}")
    if not delete:
        edited_rows.append(Rule(name=name, start=start, end=end,
                                offset=offset, floor=floor, cap=cap,
                                enabled=enabled))

bcol = st.columns(3)
if bcol[0].button("➕ Add rule"):
    edited_rows.append(Rule(name=f"Rule {len(edited_rows)+1}",
                            start="00:00", end="06:00", offset=0.0,
                            floor=40.0, cap=50.0, enabled=False))
    st.session_state.ruleset = RuleSet(rules=edited_rows)
    save_rules(st.session_state.ruleset)
    st.rerun()
if bcol[1].button("💾 Save rules"):
    st.session_state.ruleset = RuleSet(rules=edited_rows)
    save_rules(st.session_state.ruleset)
    log.info("Rules saved.")
if bcol[2].button("↺ Reset to defaults"):
    st.session_state.ruleset = RuleSet.default()
    save_rules(st.session_state.ruleset)
    st.rerun()

# ---------- main: log ---------------------------------------------------------

st.subheader("Event log")
items = log.items()[::-1]
for entry in items[:50]:
    st.text(f"{entry.when:%H:%M:%S} [{entry.level}] {entry.message}")
