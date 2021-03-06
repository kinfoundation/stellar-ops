# Horizon telegraf

This is a docker image that collects various measurements from the horizon instance.
At the moment it collects:
the ingestion distance - the diff between the core ledger and horizon ledger (as received from horizon)

In addition, this telegraf also collects StatsD metrics from the Nginx app running on the Horizon instance.

## Usage

In docker-compose, add the following service next to the horizon one.

```yaml
services:
---
version: "3"
services:
  horizon-telegraf:
    environment:
      NODE_NAME: "<node-name-goes-here, like ecosystem2300>"
      NETWORK_NAME: "<network-name-goes-here, like ecosystem>"
      REGION_NAME: "us-east-1" # region at which the infrastructure is located, for cloudwatch metrics
    image: kinecosystem/horizon-telegraf
    restart: always
    logging:
      driver: json-file
      options:
        max-size: 100m
        max-file: "3"
```
