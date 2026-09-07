# Certificados TLS

Este directorio guarda el estado persistente de Let's Encrypt creado por el
servicio `certbot` de Compose. Todo excepto este README queda ignorado por Git.

Para la primera emision, los puertos 80/443 deben estar libres y el DNS del
hostname debe apuntar directamente a este servidor:

```bash
dc --profile tls run --rm --service-ports certbot
```

Nginx monta este directorio en modo de solo lectura y usa los archivos dentro
de `live/$STOCK_SYNC_PUBLIC_HOST/`. Para renovar, detenga primero Nginx, ejecute
el mismo comando y vuelva a iniciarlo. Nunca suba este directorio a Git.
