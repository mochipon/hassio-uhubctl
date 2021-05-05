ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN \
    set -x \
    && apk add --no-cache --virtual .build-dependencies \
        build-base \
        linux-headers \
	libusb-dev \
	git \
	python3 &&\
    python3 -m ensurepip

RUN git clone https://github.com/mvp/uhubctl.git /tmp/uhubctl && \
    cd /tmp/uhubctl && make && make install

COPY . /app
WORKDIR /app
RUN pip3 install -r requirements.txt

RUN chmod a+x run.sh

CMD ["/app/run.sh"]
