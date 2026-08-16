FROM postgres:16-alpine

RUN apk add --no-cache age tzdata
COPY backup/run.sh /usr/local/bin/billing-backup
RUN chmod 0755 /usr/local/bin/billing-backup

ENTRYPOINT ["/usr/local/bin/billing-backup"]
