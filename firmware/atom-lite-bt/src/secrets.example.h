// Copy to secrets.h (never committed), and fill in. The Atom has 2.4 GHz Wi-Fi only.
#pragma once
#define WIFI_SSID "your 2.4 GHz network"
#define WIFI_PASSWORD "its password"
// Any long random string (openssl rand -base64 32), also in TARS's .env as PURIFIER_ATOM_KEY.
#define TARS_KEY "..."
// Any long random string: updates over Wi-Fi need it.
#define OTA_PASSWORD "..."
