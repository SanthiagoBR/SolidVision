# ADR-008 — PostgreSQL embarcado no instalador

**Status:** Aceito
**Origem:** spike de 2026-09-22, reconstruído pelo [RFC-033a](../rfcs/rfc-033a-linha-de-base-do-postgres-embarcado.md); [roadmap sem Docker](../rfcs/ROADMAP-standalone.md)
**Implementado por:** RFC-033 (adaptador de produção, proposto)
**Revisa:** [RFC-029 §14](../rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md), a linha sobre deploy do [ADR-002](adr-002-postgresql-em-vez-de-banco-vetorial.md) e o `AI_Context.md`

---

## Decisão

O instalador do SolidVision leva o **PostgreSQL 16.15 e o pgvector 0.8.6**, tirados do conda-forge para `win-64`, e o app sobe o banco como **processo filho**: só em loopback, numa porta alta, com autenticação scram, encoding `UTF8` e localidade `C`.

As builds são fixas, não só as versões: `postgresql=16.15=he837cf3_0`, `pgvector=0.8.6=h2466b09_0` e `libpq=16.15=h43e12c5_0`. O ambiente resolvido inteiro, com o `md5` de cada pacote, está no log de [`measure_embedded_postgres.py`](../../experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py).

O Docker fica **só como atalho de desenvolvimento**, em `pgvector/pgvector:0.8.6-pg16`. É a mesma versão maior, o mesmo pgvector, o mesmo encoding e a mesma localidade do banco embarcado. Uma tabela campo a campo confere isso (RFC-033a §6).

## Justificativa

Os motivos se dividem em duas listas, e a separação é parte da decisão.

### O que decide pelo banco

- **O Docker Desktop como pré-requisito.** Para abrir o app com o banco em container, um fotógrafo precisaria de WSL 2, virtualização ligada no firmware, privilégio de administrador para instalar e um daemon rodando antes de o app abrir. Nenhuma dessas exigências tem a ver com o que o app faz.
- **O transporte, medido.** No Docker desta máquina, toda mensagem acima de ~8 KB custa +43 ms, e todo vetor de consulta tem ~10 KB ([RFC-032 §14](../rfcs/rfc-032-geolocalizacao-e-busca-por-proximidade.md)). Esse é o piso de toda busca vetorial atravessando a porta publicada. Com o banco embarcado, a conexão é loopback nativo.
- **O ciclo de vida.** O launcher do RFC-036 precisa subir, vigiar e derrubar o banco. Com Docker, ele dependeria de um daemon que não controla e que pode não estar rodando.

### O que não decide pelo banco

A identidade de volume do [RFC-027](../rfcs/rfc-027-dispositivos-e-identidade-de-volume.md), os HDs plugados a quente, o reveal do [RFC-030](../rfcs/rfc-030-acesso-ao-arquivo.md) e o I/O de fotos atravessando a fronteira da VM são motivos verdadeiros. Eles já decidiram o que decidem, no [RFC-029 §6 e §14](../rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md): a API e o `job_runner` rodam no host.

Nenhum deles alcança o banco. O PostgreSQL nunca abre uma foto, nunca lê um GUID de volume e nunca chama o Explorer. Tanto é assim que o RFC-029 §14, com esses mesmos quatro motivos, concluiu *"com só o PostgreSQL em container"*. Um ADR que os usasse para tirar o banco do container chegaria à resposta certa pelo motivo errado, e deixaria sem explicação por que o RFC-029 concluiu o oposto.

## Por que 16, e não 17

O pgvector do conda-forge para `win-64` declara `libpq >=16.15,<17.0a0`, restrição que o passo 2 do script imprime a cada execução. Não há build do pgvector para o 17 nesse canal. Compilar uma traria toolchain MSVC e PGXS para o build do projeto, que é o custo que o [RFC-002 §1](../rfcs/rfc-002-docker-e-postgres.md) evitou ao escolher uma imagem pronta.

O desenvolvimento acompanha: testar em 17 e entregar em 16 é uma divergência que a suíte não pega. O pgvector também precisava acompanhar, e por isso a tag é fixa. A tag flutuante `pg16` já aponta para o pgvector 0.8.7.

## Por que `UTF8` e `C`

**`UTF8`.** Sem `--encoding`, o `initdb` no Windows deriva o encoding da localidade do sistema. Nesta máquina ele escolheu `WIN1252` e `Portuguese_Brazil.1252` (RFC-033a §5). Um cluster em `WIN1252` recusa qualquer nome de arquivo com `ł`, ideogramas ou emoji, e o NTFS aceita todos eles.

**`C`.** É a única localidade que dá o mesmo resultado no glibc do Debian e no MSVC do Windows por definição, e não por teste. Nenhuma atualização de biblioteca consegue mudar a ordem por baixo de um índice B-tree. O ICU nem é opção do lado embarcado, porque o PostgreSQL 16.15 do conda-forge `win-64` foi compilado sem ele (*"ICU is not supported in this build"*, lido do servidor).

## Números

O spike e o script reescrito, lado a lado. A linha de base do RFC-033 é a do script.

| medição | spike, 2026-09-22 | script reescrito, 2026-10-08 |
| --- | --- | --- |
| Head do Alembic | `f4b9e2d7c615` | `6d77379a36a1` (RFC-032) |
| Docker durante a execução | desligado | desligado (e, numa segunda execução, ligado, com o alvo da suíte provado) |
| `initdb`, uma vez por máquina | 27,2 s | **24,7 s** (29,3 s na execução com o Docker ligado) |
| Payload sem os `.pdb` | 110 MB (de 308 MB) | **123 MB** (de 308 MB, 184 MB de `.pdb` removidos) |
| `listening` → `ready` | 0,248 s | **0,216 s** |
| `alembic upgrade head` | 3,3 s | **2,5 s** |
| Suíte padrão | 1616 passed, 58 deselected | **2085 passed, 58 deselected**, alvo provado pela porta e por `xact_commit` |
| Armazenamento declarado | *"system SSD"* | HDD SATA (`ST1000DM010-2EP102`), lido do `Get-PhysicalDisk` |

Dois números do spike não se sustentam como estavam escritos:

- **O payload.** A conta do próprio log não fecha, porque 308 − 184 = 124, e não 110. Removendo só os `.pdb`, como o log diz, o resultado é 123 MB. Os ~13 MB que faltam batem com `Library/include` (12,4 MB) mais `conda-meta` (1,2 MB), o que sugere que o spike removeu mais do que registrou. O script mede só o que diz medir.
- **O disco.** A máquina das duas execuções tem um único disco, mecânico. A linha *"system SSD"* era uma constante no script do spike. O script novo lê o meio de armazenamento em vez de afirmá-lo.

## Trade-off aceito

- **123 MB no instalador.** Desses, 47 MB são DLLs do ICU, que o servidor não usa: elas vêm como dependência do libxml2. O RFC-033 pode medir o que dá para tirar.
- **Um `initdb` na primeira execução**, de 25 a 39 s neste HDD nas medições de 2026-10-08. Ele acontece uma vez por máquina.
- **O ciclo de vida do banco passa a ser código do projeto** (RFC-033), e não do Docker: primeira execução, porta ocupada, encerramento sujo, processo órfão.
- **Atualizar de versão maior exige um `pg_upgrade` que nunca foi medido.** O diretório de dados precisa deixar espaço para dois clusters lado a lado, e o volume de desenvolvimento já leva a versão maior no nome (`postgres_data_pg16`).
- **Colação `C`.** A ordem é por bytes, e o `lower()` do SQL só converte ASCII. Nenhuma consulta de produto ordena texto no SQL. O efeito conhecido é o pré-filtro do `device_reconcile`, que só serve a linhas anteriores ao RFC-027 (RFC-033a §5 e §10).

## O que este ADR revisa

Uma decisão revertida continua documentada. Nenhum dos três textos abaixo foi reescrito; cada um ganhou uma nota que aponta para cá.

- **[RFC-029 §14](../rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md):** *"processo nativo no Windows, empacotado como instalador, com só o PostgreSQL em container"*. A primeira metade continua valendo. A segunda não vale mais.
- **[ADR-002](adr-002-postgresql-em-vez-de-banco-vetorial.md):** *"Deploy mais simples. Um container, uma imagem oficial (`pgvector/pgvector:pg17`)…"*. O argumento de fundo continua de pé: um banco só, e nenhum serviço vetorial para operar ao lado. Mas esse banco agora é um processo filho do app, não um container.
- **`AI_Context.md`:** *"with only PostgreSQL in a container"*.
