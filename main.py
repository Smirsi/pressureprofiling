import streamlit as st
from streamlit import session_state as ss
import pandas as pd
import plotly.express as px
import json
import time
import paho.mqtt.client as paho
from paho import mqtt
import os

# Schrittmotor-Parameter
STEPS_PER_REV = 4096  # Schritte pro Umdrehung in half stepping, otherwise 2048 in full-step mode
BAR_PER_REV = 2
BAR_TO_STEPS = STEPS_PER_REV // BAR_PER_REV  # Schritte pro Bar

# MQTT-Konfiguration
user = "Smirsi"
pwd = "PressureProfiling8266"
host = "b8fb421c847649988582f532fabf2a84.s1.eu.hivemq.cloud"
port = 8883
publish_topic = "pressure_profile"
subscribe_topic = "espresso_machine"
msg = "hello from python script"


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


def send_to_esp32(message):
    """
    Verbindet sich mit dem Broker, sendet die Nachricht und schließt wieder.
    Wird hier auch als print ausgegeben.
    """
    try:
        client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
        client.on_connect = on_connect
        client.tls_set(tls_version=mqtt.client.ssl.PROTOCOL_TLS)
        client.username_pw_set(user, pwd)
        client.connect(host, port)
        client.on_publish = on_publish
        client.loop_start()
        time.sleep(0.5)  # kurz warten, bis die Verbindung steht
        client.publish(publish_topic, payload=json.dumps(message), qos=1)
        client.loop_stop()
        client.disconnect()
        print("Gesendet:", message)
    except Exception as e:
        print("Fehler beim Senden:", e)


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

if not ss.df.empty:
    fig = px.line(ss.df, x="time", y="pressure", markers=True)
    fig.update_layout(xaxis_title="Zeit (s)", yaxis_title="Druck (Bar)")
    col2.plotly_chart(fig, use_container_width=True)
st.divider()
st.markdown("### Pressure Profile speichern")
col1, col2, col3 = st.columns(3, vertical_alignment="bottom")
# Benutzer kann einen Dateinamen angeben (ohne .csv)
filename = col1.text_input("Dateiname", value="")
if col2.button("Pressure Profile Speichern", type='primary', use_container_width=True):
    if filename:
        file_path = os.path.join(profiles_dir, f"{filename}.csv")
        ss.df.to_csv(file_path, index=False)
        st.toast(f"Pressure Profile gespeichert!", icon="📜")
    else:
        st.toast("Bitte einen Dateinamen eingeben!", icon="❌")
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
    pressure_diff = pressure_target - pressure_current

st.divider()


def compute_motion_parameters(t1, p1, t2, p2):
    """
    Berechnet für ein Segment von t1 zu t2 (dt = t2-t1) und Druckwechsel von p1 zu p2:
      - delta pressure (dp)
      - erforderliche Umdrehungen und daraus abgeleitete Schritte (S)
      - bei einer symmetrischen (dreieckigen) Bewegungsbahn:
          a = 2 * |S| / dt^2
          v_max = |S| / dt
      - Richtung: clockwise (Druckerhöhung) oder counterclockwise (Druckreduktion)
    """
    t1 = int(t1)
    t2 = int(t2)
    p1 = float(p1)
    p2 = float(p2)

    dt = t2 - t1
    dp = p2 - p1
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
    direction = "clockwise" if steps > 0 else "counterclockwise" if steps < 0 else "none"
    return {
        "t": t1,
        "s": steps,
        "v": v_max,
        "a": acceleration
    }


# Berechne Befehle für jedes Intervall
commands = []
for i in range(len(ss.df) - 1):
    t1 = ss.df.loc[i, "time"]
    p1 = ss.df.loc[i, "pressure"]
    t2 = ss.df.loc[i + 1, "time"]
    p2 = ss.df.loc[i + 1, "pressure"]
    cmd = compute_motion_parameters(t1, p1, t2, p2)
    if cmd:
        commands.append(cmd)

# todo: rückmeldung von microcontroller hinzufügen!

st.markdown("### Start des Pressure Profilings")
pw = st.text_input('Passwort eingeben:')
if (st.button("Start des Pressure Profilings", type='primary', use_container_width=True) and not ss.df.empty
        and pw == '1245'):
    # todo: check if message size is to big (> 500)
    send_to_esp32(commands)
elif pw != '1245':
    st.toast("Falsches Passwort!", icon="❌")
elif not ss.df.empty:
    st.toast("Kein Profil erstellt!", icon="❌")
    # Simulation: Ausführung der Befehle zum vorgegebenen Zeitpunkt
    # (Die Zeitangaben im DataFrame werden hier als reale Sekunden angenommen)
    # start_sim_time = time.time()  # Simulationsstart in realer Zeit
    # print("Starte Simulation...")

    # for cmd in commands:
    #     # Warte bis zum Start des aktuellen Befehls (relativ zum Simulationsstart)
    #     target_time = start_sim_time + cmd["t_start"]
    #     while time.time() < target_time:
    #         time.sleep(0.05)
    #     # Sende den Befehl (via MQTT oder alternativ auch nur print)
    #     # print(cmd)
    #     send_to_esp32(cmd)

