/*
  Rui Santos
  Complete project details at https://RandomNerdTutorials.com/esp8266-nodemcu-stepper-motor-28byj-48-uln2003/
  
  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files.
  
  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.
  
  Based on Stepper Motor Control - one revolution by Tom Igoe
*/
// #undef  MQTT_MAX_PACKET_SIZE // un-define max packet size
// #define MQTT_MAX_PACKET_SIZE 1500  // fix for MQTT client dropping messages over 128B


#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <AccelStepper.h>
#include <vector>
#include <time.h>
#include <TZ.h>
#include <FS.h>
#include <LittleFS.h>
#include <CertStoreBearSSL.h>

// WLAN- und MQTT-Daten (bitte anpassen)
const char* ssid = "ZTE 2.4G";
const char* password = "Ace-2468";
const char* mqtt_server = "b8fb421c847649988582f532fabf2a84.s1.eu.hivemq.cloud";
const int   mqtt_port = 8883;
const char* mqtt_topic = "pressure_profile";
// MQTT-Benutzername und Passwort
// const char* mqtt_username = "hivemq.webclient.1741890832967";
// const char* mqtt_password = "0:,?859.ZHRYIjQdcabf";
const char* mqtt_username = "Smirsi";
const char* mqtt_password = "PressureProfiling8266";


const int stepsPerRevolution = 2048;  // change this to fit the number of steps per revolution

// ULN2003 Motor Driver Pins
#define IN1 5
#define IN2 4
#define IN3 14
#define IN4 12

// initialize the stepper library
AccelStepper stepper(AccelStepper::HALF4WIRE, IN1, IN3, IN2, IN4);

// A single, global CertStore which can be used by all connections.
// Needs to stay live the entire time any of the WiFiClientBearSSLs
// are present.
BearSSL::CertStore certStore;

// OLD
// WiFiClient espClient;
// PubSubClient client(espClient);

// NEW
WiFiClientSecure espClient;
PubSubClient * client;


// Struktur für einen Motorbefehl
struct MotorCommand {
  unsigned long t_start;   // Zeitpunkt (in Sekunden, relativ zum Empfang)
  unsigned long dt;        // Dauer des Intervalls (optional)
  long steps;              // Zu fahrende Schritte (positiv = Erhöhung, negativ = Reduktion)
  float v_max;             // Maximale Geschwindigkeit (Schritte/s)
  float acceleration;      // Beschleunigung (Schritte/s²)
  String direction;        // "clockwise" oder "counterclockwise" (nur zu Debugging)
};

std::vector<MotorCommand> commandQueue;
bool commandActive = false;       // Flag, ob ein Befehl gerade ausgeführt wird
unsigned long simulationStart = 0;  // Startzeitpunkt der Abarbeitung (Millisekunden)
unsigned int currentCommandIndex = 0;

void setup_wifi() {
  delay(10);
  // We start by connecting to a WiFi network
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  randomSeed(micros());

  Serial.println("");
  Serial.println("WiFi connected");
  Serial.println("IP address: ");
  Serial.println(WiFi.localIP());
}


void setDateTime() {
  // You can use your own timezone, but the exact time is not used at all.
  // Only the date is needed for validating the certificates.
  configTime(TZ_Europe_Berlin, "pool.ntp.org", "time.nist.gov");

  Serial.print("Waiting for NTP time sync: ");
  time_t now = time(nullptr);
  while (now < 8 * 3600 * 2) {
    delay(100);
    Serial.print(".");
    now = time(nullptr);
  }
  Serial.println();

  struct tm timeinfo;
  gmtime_r(&now, &timeinfo);
  Serial.printf("%s %s", tzname[0], asctime(&timeinfo));
}


void mqttCallback(char* topic, byte* payload, unsigned int length) {
  Serial.print("MQTT-Nachricht empfangen auf Topic: ");
  Serial.println(topic);

  // Ausgabe des rohen Payloads zur Debug-Ausgabe:
  String payloadStr;
  for (unsigned int i = 0; i < length; i++) {
    payloadStr += (char)payload[i];
  }
  Serial.println("Payload: " + payloadStr);

  StaticJsonDocument<2048> doc;
  DeserializationError error = deserializeJson(doc, payload, length);
  if (error) {
    Serial.print("deserializeJson() failed: ");
    Serial.println(error.c_str());
    return;
  }

  // Leere die alte Befehlsliste:
  commandQueue.clear();

  // Prüfe, ob das empfangene JSON ein Array ist:
  if (doc.is<JsonArray>()) {
    JsonArray arr = doc.as<JsonArray>();
    for (JsonObject obj : arr) {
      MotorCommand cmd;
      cmd.t_start      = obj["t"] | 0;     
      cmd.steps        = obj["s"] | 0;      
      cmd.v_max        = obj["v"] | 0.0;      
      cmd.acceleration = obj["a"] | 0.0;
      commandQueue.push_back(cmd);
      
      Serial.print("Befehl aus Array: t_start=");
      Serial.print(cmd.t_start);
      Serial.print(" s, steps=");
      Serial.print(cmd.steps);
      Serial.print(", v_max=");
      Serial.print(cmd.v_max);
      Serial.print(", accel=");
      Serial.print(cmd.acceleration);
    }
  }
  // Falls es kein Array ist, prüfe, ob es ein einzelnes Objekt ist:
  else if (doc.is<JsonObject>()) {
    JsonObject obj = doc.as<JsonObject>();
    MotorCommand cmd;
    cmd.t_start      = obj["t"] | 0;         
    cmd.steps        = obj["s"] | 0;      
    cmd.v_max        = obj["v"] | 0.0;      
    cmd.acceleration = obj["a"] | 0.0;
    commandQueue.push_back(cmd);
    
    Serial.print("Einzelner Befehl: t_start=");
    Serial.print(cmd.t_start);
    Serial.print(" s, steps=");
    Serial.print(cmd.steps);
    Serial.print(", v_max=");
    Serial.print(cmd.v_max);
    Serial.print(", accel=");
    Serial.print(cmd.acceleration);
  }
  else {
    Serial.println("Fehler: Empfangene JSON-Daten sind weder Array noch Objekt!");
    return;
  }
  
  // Setze den Startzeitpunkt der Abarbeitung (relativ zum Empfang)
  simulationStart = millis();
  currentCommandIndex = 0;
  commandActive = false;
}


void reconnect() {
  while (!client->connected()) {
    Serial.print("Versuche MQTT-Verbindung...");
    String clientId = "ESP8266Client-";   // Create a random client ID
    clientId += String(random(0xffff), HEX);
    if (client->connect(clientId.c_str(), mqtt_username, mqtt_password)) {
      Serial.println("verbunden");
      client->subscribe(mqtt_topic);
    } else {
      Serial.print("Fehler, rc=");
      Serial.print(client->state());
      Serial.println(" - Erneuter Versuch in 5 Sekunden");
      delay(5000);
    }
  }
}

void setup() {
  Serial.begin(115200);

  LittleFS.begin();
  setup_wifi();
  setDateTime();

  // NEW
  // you can use the insecure mode, when you want to avoid the certificates
  //espclient->setInsecure();

  int numCerts = certStore.initCertStore(LittleFS, PSTR("/certs.idx"), PSTR("/certs.ar"));
  Serial.printf("Number of CA certs read: %d\n", numCerts);
  if (numCerts == 0) {
    Serial.printf("No certs found. Did you run certs-from-mozilla.py and upload the LittleFS directory before running?\n");
    return; // Can't connect to anything w/o certs!
  }

  BearSSL::WiFiClientSecure *bear = new BearSSL::WiFiClientSecure();
  // Integrate the cert store with this connection
  bear->setCertStore(&certStore);

  client = new PubSubClient(*bear);

  client->setServer(mqtt_server, mqtt_port);
  client->setCallback(mqttCallback);

  // OLD
  // client.setServer(mqtt_server, mqtt_port);
  // client.setCallback(mqttCallback);
  
  // Grundeinstellungen des Steppers (Werte können durch Befehle überschrieben werden)
  stepper.setMaxSpeed(1000);
  stepper.setAcceleration(500);
}

void loop() {
  if (!client->connected()) {
    reconnect();
  }
  client->loop();
  
  // Falls keine Befehle vorliegen, nichts tun
  if (commandQueue.empty()) {
    return;
  }
  
  // Berechne verstrichene Zeit in Sekunden seit Erhalt der Befehle
  unsigned long elapsed = (millis() - simulationStart) / 1000;
  
  // Wenn aktuell kein Befehl läuft, prüfen, ob der nächste Befehl gestartet werden soll
  if (!commandActive && currentCommandIndex < commandQueue.size()) {
    MotorCommand &cmd = commandQueue[currentCommandIndex];
    if (elapsed >= cmd.t_start) {
      // Starte den Befehl: Setze Beschleunigung, maximale Geschwindigkeit und Zielposition
      stepper.setAcceleration(cmd.acceleration);
      stepper.setMaxSpeed(cmd.v_max);
      long targetPosition = stepper.currentPosition() + cmd.steps;
      stepper.moveTo(targetPosition);
      commandActive = true;
      Serial.print("Starte Befehl ");
      Serial.print(currentCommandIndex);
      Serial.print(": Zielposition ");
      Serial.println(targetPosition);
    }
  }
  
  // Falls ein Befehl aktiv ist, fahre ihn nicht-blockierend ab
  if (commandActive) {
    stepper.run();
    // Wenn Ziel erreicht
    if (stepper.distanceToGo() == 0) {
      Serial.print("Befehl ");
      Serial.print(currentCommandIndex);
      Serial.println(" abgeschlossen.");
      commandActive = false;
      currentCommandIndex++;
    }
  }
}
