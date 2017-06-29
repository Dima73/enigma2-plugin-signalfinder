#!/bin/sh


case $1 in
	update)
		SRC="https://raw.githubusercontent.com/Dima73/enigma2-plugin-signalfinder/master/src/plugin.py"
		DEST=/tmp/plugin.py
		if which curl >/dev/null 2>&1 ; then
			curl -o $DEST $SRC
		else
			echo >&2 "update-plugin: cannot find curl"
			opkg update && opkg install curl
			if which curl >/dev/null 2>&1 ; then
				curl -o $DEST $SRC
			fi
		fi
		if ! [ -f $DEST ] ; then
			echo >&2 "update: download failed"
			exit 1
		else
			if ! grep "^plugin_version " $DEST ; then
				echo >&2 "update-plugin: missing plugin_version, probably truncated file"
				exit 1
			fi
			mv /tmp/plugin.py /usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/plugin.py
			echo >&2 "need restart GUI"
		fi
		exit 0
	;;
	*)
		echo " "
		echo "Options: $0 {update}"
		echo " "
esac

echo "Done..."

exit 0

