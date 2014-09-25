#!/bin/sh

set -ex

export HOME=/var
export LANG=en_US.UTF-8

cd /var

if ! test -e mail; then
  mkdir -p mail/cur
  mkdir -p mail/new
  mkdir -p mail/tmp

  chmod -R 770 mail

  mkdir -p /var/.local/share/Mailpile
  cp -r /default /var/.local/share/Mailpile
  cp -r /.gnupg /var
fi

bash -c 'while [ ! -e /tmp/sandstorm-api ]
do
  sleep 1
done

/sandstorm-smtp-bridge/bin/sandstorm-smtp-bridge' &

/usr/bin/python /mp --rescan all
/usr/bin/python /mp --www= --wait
