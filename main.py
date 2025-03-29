import streamlit as st
from streamlit import session_state as ss
import pandas as pd
import plotly.express as px
import json
import time
import paho.mqtt.client as paho
from paho import mqtt
import os
import numpy as np
import plotly.graph_objects as go
import plotly.subplots as sp

# Schrittmotor-Parameter
STEPS_PER_REV = 200  # Schritte pro Umdrehung in half stepping, otherwise 2048 in full-step mode
BAR_PER_REV = 2
BAR_TO_STEPS = STEPS_PER_REV // BAR_PER_REV  # Schritte pro Bar

# MQTT-Konfiguration
user = "Smirsi"
pwd = "PressureProfiling8266"
host = "b8fb421c847649988582f532fabf2a84.s1.eu.hivemq.cloud"
port = 8883
publish_topic = "pressure_profile"
subscribe_topic = "espresso_machine"


# setting callbacks for different events to see if it works, print the message etc.
def on_connect(client, userdata, flags, rc, properties=None):
    print("CONNACK received with code %s." % rc)


# with this callback you can see if your publish was successful
def on_publish(client, userdata, mid, properties=None):
    print("mid: " + str(mid))


# print which topic was subscribed to
def on_subscribe(client, userdata, mid, granted_qos, properties=None):
    print("Subscribed: " + str(mid) + " " + str(granted_qos))


# print message, useful for checking if it was successful
def on_message(client, userdata, msg):
    print(msg.topic + " " + str(msg.qos) + " " + str(msg.payload))
    if msg.topic == "acknowledgment":
        print('Acknowledgment received')
        ss.ack_received = True


def send_to_esp32(message):
    """
    Verbindet sich mit dem Broker, sendet die Nachricht und schließt wieder.
    Wird hier auch als print ausgegeben.
    """
    try:
        ss.client.connect(host, port)
        ss.client.loop_start()
        time.sleep(1)  # kurz warten, bis die Verbindung steht
        ss.client.subscribe("acknowledgment", qos=0)
        ss.client.publish(publish_topic, payload=json.dumps(message), qos=1)
        print("Gesendet:", message)
        ss.client.loop_stop()
        ss.client.disconnect()

    except Exception as e:
        print("Fehler beim Senden:", e)


if "client" not in ss:
    ss.client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
    ss.client.on_connect = on_connect
    ss.client.on_publish = on_publish
    ss.client.on_subscribe = on_subscribe
    ss.client.on_message = on_message
    ss.client.tls_set(tls_version=mqtt.client.ssl.PROTOCOL_TLS)
    ss.client.username_pw_set(user, pwd)


def compute_motion_parameters(t_start, p_start, t_end, p_end):
    """
    Berechnet für ein Segment von t1 zu t2 (dt = t2-t1) und Druckwechsel von p1 zu p2:
      - delta pressure (dp)
      - erforderliche Umdrehungen und daraus abgeleitete Schritte (S)
      - bei einer symmetrischen (dreieckigen) Bewegungsbahn:
          a = 2 * |S| / dt^2
          v_max = |S| / dt
      - Richtung: clockwise (Druckerhöhung) oder counterclockwise (Druckreduktion)
    """
    t_start = int(t_start)
    t_end = int(t_end)
    p_start = float(p_start)
    p_end = float(p_end)

    dt = t_end - t_start
    dp = p_end - p_start
    if dt <= 0:
        return None
    # Umdrehungen, die nötig sind (positiv = Erhöhung, negativ = Reduktion)
    rotations = dp / BAR_PER_REV
    # Umrechnung in Schritte:
    steps = int(round(rotations * STEPS_PER_REV))
    abs_steps = abs(steps)
    # Berechnung der Beschleunigung und der maximalen Geschwindigkeit (triangular profile)
    # (Annahme: Start und Ende bei 0 Geschwindigkeit, symmetrische Beschleunigung)
    acceleration = int(round(2 * abs_steps / (dt ** 2)))
    v_max = int(round(abs_steps / dt))
    # acceleration = 2000
    # v = np.roots([1, -acceleration * dt, acceleration * abs_steps])
    # print(v)
    direction = "clockwise" if steps > 0 else "counterclockwise" if steps < 0 else "none"
    return {
        "t": t_start,
        "s": steps,
        "v": v_max,
        "a": acceleration
    }


# Streamlit GUI
st.set_page_config(page_title='Pressure Profiling', layout="wide", page_icon="☕")
st.title("Pressure Profiling")

if "df" not in ss:
    ss.df = pd.DataFrame(columns=["time", "pressure"])

# Ordner für CSV-Dateien
profiles_dir = 'profiles'
if not os.path.exists(profiles_dir):
    os.makedirs(profiles_dir)

st.markdown("### Pressure Profile laden")
col1, col2 = st.columns(2, vertical_alignment="bottom")
csv_files = [f[:-4] for f in os.listdir(profiles_dir) if f.endswith(".csv")]
if csv_files:
    selected_file = col1.selectbox("Wähle ein Pressure Profile aus", csv_files) + '.csv'
    if col2.button("Laden", type='primary', use_container_width=True):
        file_path = os.path.join(profiles_dir, selected_file)
        ss.df = pd.read_csv(file_path)
st.divider()
st.markdown("### Pressure Profile erstellen")
col1, col2, col3 = st.columns(3, vertical_alignment="bottom")
time_value = col1.number_input("Zeitpunkt (Sekunden)", min_value=0.0, step=0.1, value=0.0)
pressure_value = col2.number_input("Druck (Bar)", min_value=3.0, max_value=9.5, step=0.1, value=9.0)
if col3.button('Hinzufügen', type='primary', use_container_width=True):
    new_entry = pd.DataFrame([{"time": time_value, "pressure": pressure_value}])
    ss.df = pd.concat([ss.df, new_entry], ignore_index=True).sort_values(by="time").reset_index(drop=True)


col1, col2 = st.columns([1, 2], vertical_alignment="center", gap="medium")
ss.df = col1.data_editor(
    ss.df,
    column_config={
        "time": "Zeitpunkt [s]",
        "pressure": "Druck [bar]",
    },
    hide_index=True,
    use_container_width=True,
    num_rows="dynamic"
)
# Berechne Befehle für jedes Intervall
commands = []
if not ss.df.empty:
    fig = px.line(ss.df, x="time", y="pressure", markers=True)
    fig.update_layout(xaxis_title="Zeit (s)", yaxis_title="Druck (Bar)")
    col2.plotly_chart(fig, use_container_width=True)

    for i in range(len(ss.df) - 1):
        t1 = ss.df.loc[i, "time"]
        p1 = ss.df.loc[i, "pressure"]
        t2 = ss.df.loc[i + 1, "time"]
        p2 = ss.df.loc[i + 1, "pressure"]
        cmd = compute_motion_parameters(t1, p1, t2, p2)
        if cmd:
            commands.append(cmd)


st.divider()
st.markdown("### Pressure Profile speichern")
col1, col2, col3 = st.columns(3, vertical_alignment="bottom")
# Benutzer kann einen Dateinamen angeben (ohne .csv)
filename = col1.text_input("Profilname", value="")
if col2.button("Pressure Profile Speichern", type='primary', use_container_width=True):
    if filename:
        file_path = os.path.join(profiles_dir, f"{filename}.csv")
        ss.df.to_csv(file_path, index=False)
        st.toast(f"Pressure Profile gespeichert!", icon="📜")
    else:
        st.toast("Bitte einen Profilnamen eingeben!", icon="❌")
if col3.button("Pressure Profile zurücksetzen", type='primary', use_container_width=True):
    ss.df = pd.DataFrame(columns=["time", "pressure"])
    st.rerun()

st.divider()

st.markdown("### Anfangsdruck einstellen")
col1, col2, col3 = st.columns(3, vertical_alignment='bottom')
pressure_current = col1.number_input("Derzeitiger Druck (Bar)", min_value=3.0, max_value=9.5, step=0.1, value=9.0)
if not ss.df.empty:
    pressure_target = col2.number_input("Anfangsdruck (Bar)", value=float(ss.df['pressure'][0]), disabled=True)
else:
    pressure_target = col2.number_input("Druck (Bar)", value=9.0)
if col3.button("Druck einstellen", type='primary', use_container_width=True):
    commands = []
    t1 = 0
    p1 = pressure_current
    t2 = 5
    p2 = pressure_target
    cmd = compute_motion_parameters(t1, p1, t2, p2)
    if cmd:
        commands.append(cmd)
    send_to_esp32(commands)

st.divider()

# todo: rückmeldung von microcontroller hinzufügen!

st.markdown("### Start des Pressure Profilings")
pw = st.text_input('Passwort eingeben:')
if st.button("Start des Pressure Profilings", type='primary', use_container_width=True):
    if not ss.df.empty and pw == '1245':
        # todo: check if message size is to big (> 500)
        send_to_esp32(commands)
        st.toast(f"Pressure Profile gesendet!", icon="📜")
        toast = st.toast("3")
        time.sleep(1)
        toast.toast("2")
        time.sleep(1)
        toast.toast("1")
        time.sleep(1)
        toast.toast("Starte den Bezug!")
        st.balloons()
    elif pw != '1245':
        st.toast("Falsches Passwort!", icon="❌")
    elif not ss.df.empty:
        st.toast("Kein Profil erstellt!", icon="❌")


