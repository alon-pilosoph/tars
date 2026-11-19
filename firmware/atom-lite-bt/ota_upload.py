# Updates over Wi-Fi with the Arduino core's own espota.py: PlatformIO's still speaks the older password check, which
# the core's ArduinoOTA no longer takes. The password is read from src/secrets.h, so it's written down once.
import re
from pathlib import Path

Import("env")  # noqa: F821 - PlatformIO's
secret = re.search(r'OTA_PASSWORD "(.*)"', Path("src/secrets.h").read_text()).group(1)
espota = Path(env.PioPlatform().get_package_dir("framework-arduinoespressif32")) / "tools" / "espota.py"  # noqa: F821
env.Replace(  # noqa: F821
    UPLOADCMD=f'"$PYTHONEXE" "{espota}" -i $UPLOAD_PORT -p 3232 --auth="{secret}" -f $SOURCE',
)
