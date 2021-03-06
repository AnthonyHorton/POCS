ARG image_url=gcr.io/panoptes-exp/panoptes-utils:latest
FROM ${image_url} AS pocs-base

LABEL description="Installs the panoptes-pocs module from GitHub. \
Used as a production image, i.e. for running on PANOPTES units."
LABEL maintainers="developers@projectpanoptes.org"
LABEL repo="github.com/panoptes/POCS"

ARG panuser=panoptes
ARG userid=1000
ARG pan_dir=/var/panoptes
ARG pocs_dir="${pan_dir}/POCS"

ARG arduino_url="https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh"
ARG gphoto2_url="https://raw.githubusercontent.com/gonzalo/gphoto2-updater/master/gphoto2-updater.sh"
ARG pip_extras="[testing,google]"

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

ENV PANDIR $pan_dir
ENV PANLOG "$pan_dir/logs"
ENV PANUSER $panuser
ENV POCS $pocs_dir

# Install system dependencies.
USER root
RUN apt-get update && apt-get install --no-install-recommends --yes \
        gcc \
        gphoto2 \
        udev

# Install program dependencies.
USER ${PANUSER}
WORKDIR ${POCS}
RUN mkdir -p "${PANDIR}/scripts" && \
    cd "${PANDIR}/scripts" && \
    # Install arduino-cli.
    wget -q "${arduino_url}" -O install-arduino-cli.sh && \
    sudo BINDIR="/usr/local/bin" /bin/sh install-arduino-cli.sh && \
    rm install-arduino-cli.sh && \
    sudo chown -R "${PANUSER}":"${PANUSER}" "${POCS}"

# Install the module.
USER ${PANUSER}
# Can't seem to get around the hard-coding here.
COPY --chown=panoptes:panoptes . .
RUN "${PANDIR}/conda/bin/pip" install -e ".${pip_extras}" && \
    # Cleanup
    sudo apt-get autoremove --purge --yes && \
    sudo apt-get autoclean --yes && \
    sudo apt-get --yes clean && \
    sudo rm -rf /var/lib/apt/lists/* && \
    "${PANDIR}/conda/bin/conda" clean -tipy

USER root
