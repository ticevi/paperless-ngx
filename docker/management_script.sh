#!/usr/bin/with-contenv bash
# shellcheck shell=bash

set -e

cd "${PAPERLESS_SRC_DIR}"

if [[ $(id -u) == 0 ]] ;
then
	s6-setuidgid paperless python3 manage.py management_command "$@"
elif [[ $(id -un) == "paperless" ]] ;
then
	python3 manage.py management_command "$@"
else
	echo "Unknown user."
fi
