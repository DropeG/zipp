# Base De Servidor

Guia comun para montar automatizaciones de Zipp en un servidor. Cada automatizacion puede agregar pasos propios en su carpeta.

## Requisitos Base

- Linux con acceso SSH.
- Git.
- Python 3.11 o superior.
- Node.js 22 o superior cuando la automatizacion use `node:sqlite`.
- Acceso para crear servicios `systemd`, cron o timers.
- Directorio de despliegue recomendado: `/opt/zipp`.

## Instalacion Base

```bash
cd /opt
git clone https://github.com/DropeG/zipp.git
cd zipp

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
mkdir -p logs data
```

Editar `.env` con credenciales reales en el servidor.

## Secretos

No copiar secretos al repo. Mantenerlos solo en el servidor:

```text
.env
meli_tokens.json
```

El archivo `.env.example` sirve solo como plantilla.

## Actualizar Codigo En Servidor

```bash
cd /opt/zipp
git pull
./venv/bin/pip install -r requirements.txt
```

Despues reiniciar servicios o timers que correspondan.

## Logs

Usar `logs/` para salidas de cron y `journalctl` para servicios `systemd`.

```bash
mkdir -p /opt/zipp/logs
```

Ejemplo:

```bash
journalctl -u zipp-stock-webhook-catcher -f
```
