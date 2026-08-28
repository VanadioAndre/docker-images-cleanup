# docker-cleanup

Limpeza automática e periódica de imagens Docker não utilizadas, pensada
para servidores de CI/CD que acumulam imagens de build ao longo do tempo.

## O que o script faz

1. Remove imagens **dangling** (`<none>:<none>`) — sempre seguro.
2. Para cada repositório (ex: `registry.empresa.com/app`), mantém as
   `--keep-last` tags mais recentes e remove as demais **desde que**
   tenham mais de `--max-age-days` dias.
3. **Nunca** remove uma imagem em uso por um container (rodando ou parado).
4. Tags listadas em `--exclude-tag` (default: `latest`) nunca são removidas.
5. Repositórios listados em `--exclude-repo` são ignorados por completo.
6. Suporta `--dry-run` para simular sem apagar nada — recomendado antes
   de colocar em produção.
7. Loga em arquivo com rotação (5 arquivos de até 5MB) e no stdout.

## Permissões

O script tenta escrever logs em `/var/log/docker-cleanup/` por padrão.
Se rodar como usuário comum (não root) e essa pasta não existir, o
script agora cai automaticamente para `~/.local/share/docker-cleanup/`
e avisa no stderr — então não quebra a execução.

Para usar o caminho padrão em `/var/log`, crie a pasta uma vez com o
dono correto:

```bash
sudo mkdir -p /var/log/docker-cleanup
sudo chown $USER /var/log/docker-cleanup   # ou o usuário/serviço que vai rodar o script
```

Também é preciso que o usuário que roda o script tenha permissão para
falar com o Docker (root ou membro do grupo `docker`):

```bash
sudo usermod -aG docker $USER   # relogar depois para o grupo ter efeito
```

## Testar antes de agendar

```bash
python3 docker_cleanup.py --dry-run --verbose
```

Revise o log gerado (`/var/log/docker-cleanup/docker-cleanup.log` por
padrão) para confirmar que as imagens candidatas fazem sentido antes de
rodar sem `--dry-run`.

## Instalação

```bash
sudo mkdir -p /opt/scripts/docker-cleanup
sudo cp docker_cleanup.py /opt/scripts/docker-cleanup/
sudo chmod +x /opt/scripts/docker-cleanup/docker_cleanup.py
```

## Agendamento — opção recomendada: systemd timer

Um `systemd timer` com `OnUnitActiveSec=10d` garante "a cada 10 dias"
de fato (mesmo após reboots, com `Persistent=true`), diferente do cron
que só permite agendamento por dia-do-mês/dia-da-semana fixos.

```bash
sudo cp docker-cleanup.service docker-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now docker-cleanup.timer

# Verificar próxima execução:
systemctl list-timers docker-cleanup.timer

# Ver logs de execução:
journalctl -u docker-cleanup.service -f
```

## Alternativa: cron (aproximação de 10 em 10 dias)

Se preferir cron (menos preciso, mas mais simples):

```cron
# Roda todo dia 1, 11 e 21 às 03:00 (aproximação de ~10 dias)
0 3 1,11,21 * * /usr/bin/python3 /opt/scripts/docker-cleanup/docker_cleanup.py --max-age-days 10 --keep-last 3 >> /var/log/docker-cleanup/cron.log 2>&1
```

## Parâmetros úteis

| Parâmetro          | Default   | Descrição                                              |
|--------------------|-----------|---------------------------------------------------------|
| `--max-age-days`   | `10`      | Idade mínima para uma imagem virar candidata à remoção  |
| `--keep-last`      | `3`       | Nº mínimo de tags recentes a manter por repositório     |
| `--exclude-repo`   | (vazio)   | Repositórios a nunca limpar, separados por vírgula      |
| `--exclude-tag`    | `latest`  | Tags a nunca remover, separadas por vírgula             |
| `--dry-run`        | desligado | Simula sem remover nada                                 |
| `--verbose`        | desligado | Log em nível DEBUG                                      |

Exemplo com repositório crítico protegido:

```bash
python3 docker_cleanup.py \
  --max-age-days 10 \
  --keep-last 5 \
  --exclude-repo registry.empresa.com/keve-core \
  --exclude-tag latest,prod
```