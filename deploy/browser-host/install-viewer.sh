#!/bin/bash
# Run on the browser's Sprite by setup.sh, with its network policy lifted: Apache Guacamole for a hand-over (guacd,
# built from source since Ubuntu has no package, and its web app in Tomcat 9), and x11vnc for guacd to see the screen
# through. Skips what's already there.
set -euo pipefail
GUAC=1.6.0
TOMCAT=9.0.124

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq x11vnc x11-xserver-utils default-jre-headless \
  build-essential libcairo2-dev libjpeg-turbo8-dev libpng-dev libtool-bin uuid-dev libvncserver-dev libssl-dev \
  libwebp-dev >/dev/null

if ! /usr/local/sbin/guacd -v 2>/dev/null | grep -q "$GUAC"; then
  mkdir -p /tmp/guac && cd /tmp/guac
  curl -fsSLo server.tgz "https://downloads.apache.org/guacamole/$GUAC/source/guacamole-server-$GUAC.tar.gz"
  tar xzf server.tgz && cd "guacamole-server-$GUAC"
  CFLAGS="-O2 -Wno-error" ./configure --prefix=/usr/local --disable-guacenc --disable-guaclog >/dev/null
  make -j8 >/dev/null 2>&1
  sudo make install >/dev/null && sudo ldconfig
fi
/usr/local/sbin/guacd -v

mkdir -p ~/guac/home && cd ~/guac
if ! [ -x tomcat/bin/catalina.sh ]; then
  curl -fsSLo /tmp/tomcat.tgz "https://downloads.apache.org/tomcat/tomcat-9/v$TOMCAT/bin/apache-tomcat-$TOMCAT.tar.gz"
  # Without Tomcat's own sample apps: Guacamole is all it serves.
  tar xzf /tmp/tomcat.tgz --exclude="apache-tomcat-$TOMCAT/webapps/*" && mv "apache-tomcat-$TOMCAT" tomcat
  mkdir -p tomcat/webapps
fi
[ -f tomcat/webapps/guacamole.war ] ||
  curl -fsSLo tomcat/webapps/guacamole.war "https://downloads.apache.org/guacamole/$GUAC/binary/guacamole-$GUAC.war"
# Tomcat on the Sprite's localhost only, with no shutdown port.
sed -i 's|<Server port="8005" shutdown="SHUTDOWN">|<Server port="-1" shutdown="SHUTDOWN">|;
        s|<Connector port="8080" protocol="HTTP/1.1"|<Connector port="8081" address="127.0.0.1" protocol="HTTP/1.1"|' \
  tomcat/conf/server.xml
echo "Guacamole $GUAC in Tomcat $TOMCAT"
