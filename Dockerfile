# Build the open-source converter image first; see README.md.
ARG CONVERTER_IMAGE=docx-print-converter:0.1.0
FROM ${CONVERTER_IMAGE}
USER root
ENV DEBIAN_FRONTEND=noninteractive HOME=/data LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    STATION_QUEUE=M437 STATION_USER=office \
    STATION_DEV="airscan:w0:HP M437" \
    STATION_DOCX_CONVERTER=/usr/local/bin/docx-print-convert
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini cups cups-client cups-filters cups-ipp-utils \
      sane-utils sane-airscan libusb-0.1-4 \
      python3-flask python3-pil python3-numpy python3-opencv python3-img2pdf \
      ocrmypdf tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd \
      tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
      libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
      unoconv ghostscript qpdf poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/nas-office-station /data /run/cups /var/spool/cups/tmp \
 && chown root:lp /var/spool/cups/tmp \
 && chmod 1770 /var/spool/cups/tmp
COPY app/ /opt/nas-office-station/app/
COPY scripts/ /opt/nas-office-station/scripts/
COPY LICENSE THIRD_PARTY_NOTICES.md /opt/nas-office-station/
RUN chmod 755 /opt/nas-office-station/scripts/*.sh
WORKDIR /opt/nas-office-station
EXPOSE 8071
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/opt/nas-office-station/scripts/healthcheck.sh"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/nas-office-station/scripts/entrypoint.sh"]
