#!/bin/bash
curl -k https://get.helm.sh/helm-$(curl -L --silent --show-error --fail "https://get.helm.sh/helm-latest-version" 2>&1 | grep ^v[0-9])-linux-amd64.tar.gz  --output /srv/www/htdocs/helm/helm-latest-linux-amd64.tar.gz
curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS  --output /srv/www/htdocs/helm/KEYS
chmod 0644 /srv/www/htdocs/helm/KEYS /srv/www/htdocs/helm/helm-latest-linux-amd64.tar.gz

