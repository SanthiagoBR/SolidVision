# RFC-033a — Linha de Base do PostgreSQL Embarcado

**Status:** Implementado
**Etapa:** 0 do [roadmap sem Docker](ROADMAP-standalone.md#etapa-0--reconstruir-o-que-se-perdeu)
**Depende de:** RFC-002 (Compose), RFC-004 (configuração tipada), RFC-007 (Alembic), RFC-018 (extensão e índice HNSW), RFC-019 (fixture `db_session`), RFC-029 (§6 e §14, onde cada processo roda), RFC-032 (head atual, `6d77379a36a1`)
**Bloqueia:** RFC-033 (PostgreSQL embarcado), cujo critério de não regressão precisa de algo contra o que comparar
**Migration:** não. Nenhuma revisão nova; o banco de desenvolvimento troca de volume e é recarregado (§7)
**Medição:** `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py` — medido. `measure_embedded_postgres.log` é a execução com o Docker desligado (2026-10-08 22:32), e `measure_embedded_postgres_parity.log` é a execução com o Docker ligado e `--parity` (22:47). Os dois ficam ao lado do script e fora do git, como o log de todo experimento. O log original do spike foi preservado na mesma pasta como `measure_embedded_postgres.spike-2026-09-22.log` e está transcrito no Apêndice A

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os únicos números medidos neste documento são os do spike de 2026-09-22, transcritos no Apêndice A, e os do banco de desenvolvimento, lidos em 2026-10-08 para escrever §2.3 e §2.4. Cada um diz de onde veio.
>
> **Isso foi feito.** Na implementação, em 2026-10-08, cada `TBM` virou um número, escrito à vista da frase original. Onde a medição contrariou o texto, a correção está registrada em §14.

---

## 1. Contexto

Em 2026-09-22, um spike mostrou que o PostgreSQL 16.15 com pgvector 0.8.6, tirados do conda-forge para win-64, sobem num Windows com o Docker desligado e passam a suíte inteira do projeto. Com isso, decidiu-se que o SolidVision leva o banco dentro do instalador, e o [roadmap sem Docker](ROADMAP-standalone.md) foi escrito sobre essa decisão. O RFC-033 vai transformar o spike em código de produção.

O roadmap também registrou o que sobrou do spike, que foi só um log. O script que o produziu e o ADR que deveria registrar a decisão não existem, e a imagem de desenvolvimento roda outra versão maior. Esta é a Etapa 0. O roadmap explica por que ela vem primeiro: *"sem o script, a Etapa 1 não tem como provar que não regrediu em relação ao spike."*

Este RFC é pequeno de propósito. Ele não escreve uma linha de código de backend, não cria migration e não toca no `Settings`. Reconstrói a linha de base, registra a decisão e põe o desenvolvimento na mesma versão da produção. Ao fazer isso, corrige três coisas que o roadmap presumiu (§2.2, §2.3 e §2.5).

## 2. Problema

### 2.1 Um número sem script é uma afirmação

Os números do spike estão citados no roadmap: `initdb` em 27,2 s, payload de 110 MB e 1616 testes passando. **Nenhum deles pode ser reproduzido hoje.** O log cita `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py` na primeira linha, e esse arquivo não está em nenhum commit, branch ou stash (verificado em 2026-10-08). O script foi escrito dentro do repositório, rodou e sumiu antes de ser versionado. A única linha dele que sobreviveu é a 173, `tar.extract(member, work)`, e só porque o Python emitiu um aviso de depreciação sobre ela.

A convenção do README diz que um RFC que passa a implementado sem que nenhum `TBM` tenha virado número *"não foi implementado: foi presumido"*. Sem o script, o RFC-033 seria julgado contra números que só existem como texto, e o resultado seria o mesmo.

**O log também não está versionado.** O `.gitignore` ignora `*.log`, como acontece com o log de todo experimento desde o RFC-030 (o RFC-031 §16 registra a convenção). O link do roadmap para o log funciona nesta máquina e em nenhum clone. Se o disco falhar, o último registro do spike vai junto.

### 2.2 O log não diz tudo o que o cluster era

O critério do roadmap, *"reproduz o log de ponta a ponta"*, não basta, porque o log omite escolhas que mudam o resultado:

| o que o log não registra | por que importa |
| --- | --- |
| Encoding e localidade do `initdb` | O passo 4 diz só *"scram auth, password file"*. Sem `--encoding` e `--locale`, o `initdb` no Windows herda a localidade do sistema (§5) |
| A porta | O log diz *"high port"*, sem dizer qual nem como foi escolhida |
| Como a suíte foi apontada para o cluster | O passo 7b diz *"against this cluster"*, sem dizer como. Com o Docker ligado, uma variável de ambiente com o nome errado faria a suíte passar contra o banco errado (§4.2) |
| A versão do micromamba pedida | O log registra a que veio (`2.9.0`), não a que foi pedida |
| O ambiente resolvido | Aparecem as três builds principais, mas não as dependências transitivas (ICU, OpenSSL, zlib…) |

O script reescrito reproduz cada linha do log **e** registra o que ele omitia. Uma reprodução que repetisse as omissões reproduziria também o defeito delas.

### 2.3 A versão maior não é a única divergência

O roadmap nomeou a divergência de versão maior. A leitura do banco de desenvolvimento em 2026-10-08 mostra outras:

| | dev hoje (lido em 2026-10-08) | embarcado (log do spike) |
| --- | --- | --- |
| PostgreSQL | 17.10, Debian, gcc | 16.15, Visual C++ |
| pgvector | **0.8.5** | **0.8.6** |
| Encoding | `UTF8` | não registrado |
| Localidade (`datcollate`, `datctype`) | `en_US.utf8`, `en_US.utf8`, provedor libc | não registrada |
| Head do Alembic | `6d77379a36a1` (RFC-032) | `f4b9e2d7c615` (anterior ao RFC-032) |
| Suíte padrão | 2047 passed (RFC-032 §13) | 1616 passed |
| Transporte | Porta publicada pelo Docker: +43 ms por mensagem acima de ~8 KB (RFC-032 §14) | Loopback nativo |

**O pgvector também diverge, e a correção que o roadmap propõe aumentaria essa divergência.** A tag flutuante `pgvector/pgvector:pg16` aponta hoje para o pgvector **0.8.7**, publicado em 2026-10-01 segundo o Docker Hub. Trocar `pg17` por `pg16`, como está escrito no roadmap, consertaria a versão maior do servidor. Em troca, o pgvector do dev deixaria de estar atrasado (0.8.5) para ficar adiantado (0.8.7) em relação ao embarcado (0.8.6). Existem tags fixas: `0.8.6-pg16` foi publicada em 2026-08-13.

**A localidade é a divergência que a suíte não pega.** Os 1616 testes passaram num cluster cuja localidade ninguém registrou. Uma suíte verde prova que os testes existentes passam. Não prova que ordenação de texto, `lower()` e o conjunto de caracteres aceitos são os mesmos nos dois bancos (§6).

### 2.4 Trocar a imagem não troca o volume

Um diretório de dados criado pelo PostgreSQL 17 não abre no 16: o servidor se recusa a subir. O `pg_upgrade` também não serve, porque só atualiza para uma versão maior mais nova. Trocar a tag no `docker-compose.yml` e rodar `docker compose up` deixa o desenvolvimento sem banco.

O banco de desenvolvimento é pequeno, mas não é descartável. Em 2026-10-08 ele tinha 60 imagens com embedding CLIP real, 1 dispositivo e 1 job. Entre elas estão os 40 arquivos DJI reais do RFC-032 §13, com as posições que o `exif_backfill` gravou. Reindexar custaria pouca inferência (~27 s, a ~450 ms por imagem), mas exige que a pasta real esteja acessível nesta máquina, e o RFC-032 §2.3 registra que ela foi *"fornecida pelo usuário"*. Um dump não depende disso.

Os 200 MB que `images` ocupa hoje não são dado. São inchaço das medições de planejador que inseriram dezenas de milhares de linhas e desfizeram as transações (RFC-031 §16, RFC-032 §6.1.1). O dump leva só as 60 linhas vivas.

### 2.5 Os motivos do ADR, e a quem eles se aplicam

O roadmap pede que o ADR-008 registre cinco motivos: a identidade de volume do RFC-027, os HDs plugados a quente, o reveal do RFC-030, o I/O atravessando a VM e o Docker Desktop como exigência para um fotógrafo.

Os quatro primeiros são verdadeiros, mas não justificam tirar o **banco** do container. Eles explicam por que a **API e o `job_runner`** rodam no host, e essa decisão já foi tomada no RFC-029 §6 e §14. O PostgreSQL nunca abre uma foto, nunca lê um GUID de volume e nunca chama o Explorer. Tanto é assim que o RFC-029 §14, com esses mesmos quatro motivos, concluiu: *"processo nativo no Windows, empacotado como instalador, com só o PostgreSQL em container"*. O `AI_Context.md` repete essa frase até hoje.

Um ADR que usasse esses quatro motivos para justificar o banco embarcado chegaria à resposta certa pelo motivo errado, para usar a expressão do RFC-032 §9. Também deixaria sem explicação por que o RFC-029 concluiu o oposto com os mesmos argumentos. Para o banco, os motivos que valem são outros:

- **O Docker Desktop como pré-requisito.** Para abrir o app, um fotógrafo precisaria de WSL 2, de virtualização ligada no firmware, de privilégio de administrador para instalar e de um daemon rodando antes de o app abrir.
- **O transporte, que foi medido.** No Docker desta máquina, toda mensagem acima de ~8 KB custa +43 ms, e todo vetor de consulta tem ~10 KB (RFC-032 §14). Esse é o piso de toda busca vetorial no desenvolvimento.
- **O ciclo de vida.** O launcher do RFC-036 precisa subir, vigiar e derrubar o banco. Com Docker, ele dependeria de um daemon que não controla e que pode não estar rodando.

Portanto, o ADR-008 revisa uma frase do RFC-029 e outra do `AI_Context.md`, e diz isso explicitamente (§8). Pelas convenções do README, uma decisão revertida continua documentada.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Script | Reescrito em `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py` e **versionado no mesmo commit que o documento** que cita seus números (§4) |
| O que ele reproduz | Cada passo do log, contra o head atual (`6d77379a36a1`), mais o que o log omitia (§2.2, §4.1) |
| Versões | micromamba, `postgresql`, `pgvector` e `libpq` fixados por versão **e por build**; o ambiente explícito, com `md5`, vai para o log (§4.1) |
| Prova do alvo | A suíte só conta se um subprocesso com o mesmo ambiente confirmar a porta e a versão do cluster (§4.2) |
| Encoding | `UTF8` nos dois bancos (§5) |
| Localidade | `C` nos dois bancos, provedor libc (§5) |
| Imagem de dev | `pgvector/pgvector:0.8.6-pg16`, uma tag fixa. **Não** `pg16`, que hoje traz o pgvector 0.8.7 (§2.3) |
| Volume de dev | `postgres_data_pg16`, novo. O volume do 17 fica intacto (§7) |
| Dados de dev | Esquema por `alembic upgrade head` e dados por `pg_dump --data-only`, conferidos por contagem e `md5` (§7) |
| Paridade | Uma tabela campo a campo, com as divergências aceitas declaradas antes da medição (§6) |
| ADR-008 | Separa os motivos que valem para o banco dos que já decidiram onde a API roda (§2.5, §8) |
| Log do spike | Transcrito no Apêndice A. `*.log` continua fora do git |
| Backend | **Nada muda**: nem código, nem migration, nem `Settings` (este último é do RFC-034) |

## 4. O script

### 4.1 Passo a passo, contra o log

Cada passo continua imprimindo o próprio tempo, no formato `[x s]` do original.

| passo do log | o que o script reescrito faz | o que acrescenta |
| --- | --- | --- |
| 1. micromamba | Baixa o binário avulso, sem instalar | Versão fixada (`2.9.0`) e `sha256` conferido. A extração usa `filter="data"`, o que elimina o `DeprecationWarning` da primeira linha do log |
| 2. materializar | `postgresql=16.15=he837cf3_0`, `pgvector=0.8.6=h2466b09_0`, `libpq=16.15=h43e12c5_0`, só conda-forge, `win-64` | Fixa builds exatas, e não só versões. Grava no log a lista explícita do ambiente com `md5` (`micromamba list --explicit --md5`), porque é ela, e não a spec, que reproduz o ambiente. Continua imprimindo a restrição `pgvector → libpq`, que é a evidência do ADR-008 para a versão 16 |
| 3. poda | Remove os `.pdb` e mede o tamanho antes e depois | — |
| 4. `initdb` | scram e arquivo de senha | `--encoding=UTF8 --locale=C` explícitos (§5). A linha em que o `initdb` anuncia localidade e encoding vai para o log |
| 5. subida | Loopback, porta alta; intervalo de `listening` a `ready` lido do log do servidor | A porta, pedida livre ao sistema, vai para o log. `log_line_prefix` com milissegundos, sem o qual o intervalo não pode ser medido |
| 6. extensão e HNSW | `CREATE EXTENSION vector`, distância de cosseno com `<=>`, índice HNSW com `vector_cosine_ops` | — |
| 7a. Alembic | `alembic upgrade head` | O head esperado agora é `6d77379a36a1`. Confere as três colunas e os três `CHECK`s do RFC-032 |
| 7b. suíte | Suíte padrão | Prova de que a suíte falou com este cluster (§4.2). Contagem de passed, deselected e failed |
| 8. fatos *(novo)* | — | Os campos de §6, que o modo `--parity` compara com o banco de dev |
| veredito | `initdb`, payload, suíte, Docker | Acrescenta `listening → ready`, tempo do Alembic, contagem da suíte, porta, encoding e localidade |

### 4.2 Provar que a suíte falou com o cluster certo

A suíte é apontada para o cluster embarcado por variáveis de ambiente (`DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_USER`, `DATABASE_PASSWORD` e `DATABASE_NAME`), que o `pydantic-settings` põe acima do `.env` (RFC-004). Nenhum código muda. O risco é outro: se uma variável sair com o nome errado, o `Settings` cai no `.env`, o `.env` aponta para o Docker, e a suíte passa contra o banco errado e imprime o mesmo `passed`.

O spike evitou isso por acaso. O Docker estava desligado, e a suíte não tinha outro banco para encontrar. O script reescrito não depende disso. Antes da suíte, um subprocesso com o mesmo ambiente importa o `settings` do projeto, abre a engine do projeto e consulta `inet_server_port()` e `version()`. O script aborta se a porta não for a do cluster ou se a versão não contiver `16.15` e `Visual C++`. Depois da suíte, o contador de transações do banco (`pg_stat_database.xact_commit`) precisa ter subido. Os dois resultados vão para o log.

A execução que reproduz o spike continua sendo feita com o Docker desligado, porque é ela que sustenta a frase *"Docker: not running, not required"*. A verificação acima é o que permite rodar o modo `--parity` (§6) com o Docker ligado sem contaminar a medição.

### 4.3 Onde ele roda e o que deixa para trás

- **Diretório de trabalho** definido por `--work`, com padrão num diretório temporário novo. O spike rodou no diretório temporário de uma sessão de trabalho que não existe mais, e é por isso que o caminho registrado no log não leva a lugar nenhum.
- **O cluster é parado num `finally`**, sempre, inclusive quando um passo falha. Ao final, o script confere na lista de processos que nenhum `postgres.exe` ficou para trás, que é o mesmo critério que o RFC-036 vai exigir do launcher.
- **`--keep`** preserva binários e cluster para uma segunda execução. Com ele, os passos 1 a 4 são reaproveitados, e o log diz que foram.
- **`--initdb-defaults`** roda um `initdb` extra, sem flags de encoding nem de localidade, num diretório descartável, e registra o que ele escolheu (§5).
- **Nenhuma dependência nova.** Roda no Python do backend (`backend/.venv`). Download, tar, subprocess e hashlib vêm da biblioteca padrão, e o acesso ao banco usa o `psycopg` que o projeto já tem.

### 4.4 O que ele não mede

Subida a quente sob um adaptador, recuperação de encerramento sujo, porta ocupada, antivírus e caminhos com acento no diretório de dados são critérios do RFC-033. Medi-los aqui transformaria a Etapa 0 no RFC-033 sem o adaptador. O script mede o que o spike mediu, mais o que o spike omitiu, contra o head de hoje.

## 5. Encoding e localidade: UTF8 e C nos dois bancos

**Encoding.** Sem `--encoding`, o `initdb` no Windows deriva o encoding da localidade do sistema. Numa máquina em português do Brasil, a localidade é `Portuguese_Brazil.1252`, e o encoding esperado é `WIN1252`. O que o `initdb` escolhe sem flags nesta máquina era `TBM`, e o script registra isso com `--initdb-defaults`. **Medido: `WIN1252`, com `datcollate` e `datctype` `Portuguese_Brazil.1252`**, como esperado. Um cluster em `WIN1252` recusa qualquer caractere fora dessa página de código. Um nome de arquivo com `ł`, `ő`, ideogramas ou emoji não poderia ser gravado em `images.relative_path`, e o NTFS guarda nomes em UTF-16, então qualquer um deles pode aparecer num HD. A suíte passaria nos dois casos, porque os acentos do português existem no `WIN1252`. **`UTF8` é obrigatório nos dois bancos.**

**Localidade.** Há três candidatos, e só um dá o mesmo resultado nos dois lados:

| candidato | Debian (Docker) | Windows (embarcado) | problema |
| --- | --- | --- | --- |
| Localidade linguística da libc (`en_US.utf8`, `Portuguese_Brazil.1252`) | existe | outro nome, outra implementação | Não existe uma localidade da libc com o mesmo nome e o mesmo comportamento no glibc e no MSVC |
| ICU (`und`) | ICU do Debian | ICU do conda-forge | O recipe do conda-forge declara `icu` para todas as plataformas, mas a versão não é a do Debian. A ordem muda entre versões do ICU, e cada atualização do instalador que trouxer outro ICU exige `REINDEX` dos índices de texto, ou os corrompe em silêncio |
| `C` | existe | existe | Nenhum: ordem por bytes, caixa só em ASCII, nenhuma biblioteca externa |

> **Corrigido na implementação (§14).** Do lado embarcado, o ICU não é nem candidato. O PostgreSQL 16.15 do conda-forge `win-64` foi compilado sem ele e responde *"ICU is not supported in this build"*. O ICU 78.3 que aparece no ambiente é dependência do `libxml2`, não do servidor.

**A escolha é `C` nos dois.** É a única em que "mesmo resultado" vale por definição, e não por teste. É também a única que nenhuma atualização futura de biblioteca consegue mudar por baixo de um índice B-tree, que é o risco que mais importa num app desktop atualizado sem supervisão.

O custo foi conferido no código antes da decisão:

- **Ordenação por texto.** Nenhuma consulta de produto ordena por texto no SQL. Os `order_by` dos repositórios são por distância, id, célula do mapa e `created_at`. A única ordenação por texto é o `ORDER BY path` de `device_reconcile._rows_under()`, que é interna. Ordenar nomes para o usuário é trabalho da interface, que tem `Intl.Collator`.
- **`lower()`.** Com `C`, o `lower()` do SQL só converte ASCII. O `device_reconcile` usa `lower(path) LIKE lower(:root) || '/%'` como pré-filtro, e uma raiz digitada como `D:/fotos/água` deixaria de encontrar linhas gravadas como `D:/fotos/Água`, que hoje encontra. Mas o `device_reconcile` é o passo 2 da migração do RFC-027, que serve só para linhas indexadas **antes** do RFC-027, e uma instalação nova não tem nenhuma. O efeito fica registrado aqui, sem correção (§10).
- **`LIKE` com prefixo.** Com `C`, um B-tree comum atende `LIKE 'prefixo%'`, o que em `en_US.utf8` exige `text_pattern_ops` (RFC-031 §15). Isso não muda nenhuma decisão de hoje, porque o RFC-031 mostrou que o `Seq Scan` tinha outra causa, mas elimina uma condição da próxima decisão sobre esse índice.

No Docker, a escolha entra por `POSTGRES_INITDB_ARGS`, que só vale quando o volume é criado. Por isso a hora de aplicá-la é a de §7: o volume vai ser criado de novo de qualquer forma.

## 6. Paridade é uma tabela, não uma suíte verde

O modo `--parity` lê os dois bancos e imprime uma linha por campo, com o valor de cada lado e o veredito:

| campo | como é lido | regra |
| --- | --- | --- |
| Versão maior | `server_version_num / 10000` | igual |
| Versão menor | `server_version` | registrada (§6.1) |
| pgvector | `pg_extension.extversion` | igual |
| Encoding | `pg_database.encoding` | `UTF8` nos dois |
| Provedor, `datcollate`, `datctype` | `pg_database` | libc, `C` e `C` nos dois |
| Ordenação | `ORDER BY` de uma lista fixa de nomes, com maiúsculas, minúsculas, acentos, dígitos, `_` e espaço | sequência idêntica |
| `lower()` | sobre a mesma lista | resultado idêntico |
| Caminho fora do `WIN1252` | ida e volta de `Łódź/東京/✈.jpg` pelo `psycopg` | volta byte a byte |
| `hnsw.ef_search` | `SHOW`, com a extensão carregada na sessão | igual (40, o padrão que o RFC-032 §6.1.1 mediu) |
| Head do Alembic | `alembic_version` | igual |

### 6.1 Divergências aceitas, declaradas antes da medição

- **A versão menor do PostgreSQL.** A tag `0.8.6-pg16` congelou na versão 16.x vigente em agosto de 2026 (qual, `TBM`; **medido: 16.15**, a mesma do embarcado, e a divergência não chegou a acontecer), e o embarcado é 16.15. Versões menores do PostgreSQL só trazem correções e não mudam o formato em disco nem a semântica. A diferença é registrada, não exigida.
- **Compilador e sistema operacional** (gcc no Debian contra MSVC no Windows). É exatamente o que está sendo comparado, não um defeito.
- **O transporte.** Os +43 ms do Docker pertencem ao desenvolvimento e continuam lá. Qualquer latência medida no dev é tomada no servidor, como o RFC-032 já faz.

Qualquer outra diferença na tabela é um defeito desta etapa, e a etapa só termina quando ele for resolvido.

## 7. Trocar o dev de 17 para 16 sem perder o que custou tempo

1. **Antes, no 17:** contagem de linhas por tabela, `md5` do conjunto de embeddings ordenado por id, `md5` das posições e head do Alembic. O cálculo do `md5` dos embeddings é o mesmo do RFC-032 §13, que registrou `67cecd11d55b5ee3c6d2d24e966c7fc4` para as mesmas 60 linhas.
2. **Dump** com `pg_dump --data-only --exclude-table-data=alembic_version`, usando o `pg_dump` do próprio contêiner 17. O arquivo fica fora do repositório: o `.gitignore` não cobre `*.sql`, e o dump contém os caminhos do acervo.
3. **`docker-compose.yml`:** imagem `pgvector/pgvector:0.8.6-pg16`, `POSTGRES_INITDB_ARGS` e o volume renomeado para `postgres_data_pg16`.
4. **`docker compose up -d`:** sobe com o volume novo, e o `init-pgvector.sql` cria a extensão.
5. **`alembic upgrade head`.** O esquema vem das migrations, não do dump. Isso prova as onze migrations no 16 e evita carregar DDL escrita pelo 17.
6. **Carga** com `psql -v ON_ERROR_STOP=1`, para que qualquer erro aborte em vez de ser engolido. O `pg_dump` 17 escreve no cabeçalho um `SET transaction_timeout`, parâmetro que o 16 não conhece, e deve ser preciso remover essa linha antes da carga (`TBM`: confirmar). Se alguma linha for removida, isso fica registrado. **Confirmado:** com a linha, o 16 responde `unrecognized configuration parameter "transaction_timeout"`, e o `ON_ERROR_STOP` aborta antes de qualquer `COPY`. A linha 13 do dump foi a única removida (§14).
7. **Depois, no 16:** as mesmas contagens e os mesmos `md5`.
8. **Suíte padrão e `pytest -m slow`** no dev novo.

**O volume do 17 não é apagado por este RFC.** Renomear o volume no Compose deixa `solidvision_postgres_data` intacto, e reverter o `docker-compose.yml` volta a ligá-lo. Apagá-lo é um passo manual, depois da validação de §13. A Etapa 0 não automatiza um passo que não se desfaz.

O nome com a versão maior tem motivo. O roadmap lista como risco do RFC-033 deixar *"espaço para dois clusters lado a lado"* quando vier o 17, e o dev passa a seguir a mesma convenção.

## 8. O ADR-008

`docs/adr/adr-008-postgresql-embarcado.md` segue o formato dos outros sete:

- **Decisão.** O instalador leva o PostgreSQL 16.15 e o pgvector 0.8.6 (conda-forge, win-64), e o app sobe o banco como processo filho. O Docker fica só como atalho de desenvolvimento.
- **Justificativa**, em duas listas, como §2.5 exige: o que vale para o banco (o pré-requisito do Docker Desktop, o transporte medido e o ciclo de vida) e o que não vale (os quatro motivos do RFC-029, que decidem onde a API e o `job_runner` rodam).
- **Por que 16, e não 17.** A restrição `libpq >=16.15,<17.0a0` do pgvector no conda-forge win-64, que o passo 2 do script imprime.
- **Números.** Os do spike e os do script reescrito, lado a lado, cada um com a data e o head contra o qual rodou.
- **Trade-off aceito.** 110 MB no instalador; um `initdb` de ~27 s na primeira execução; o ciclo de vida do banco passa a ser código do projeto (RFC-033), e não do Docker; atualizar de versão maior exige um `pg_upgrade` que nunca foi medido; colação `C` (§5).
- **O que ele revisa.** O RFC-029 §14 (*"com só o PostgreSQL em container"*), o ADR-002 (*"Deploy mais simples. Um container…"*) e o `AI_Context.md`. Os três recebem uma nota que aponta para o ADR-008, e nenhum é reescrito.

## 9. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Trocar `pg17` por `pg16` na tag | `pg16` traz hoje o pgvector 0.8.7 e muda sozinha a cada release. Seria trocar uma divergência por outra (§2.3) |
| Manter o 17 no dev e testar o 16 só pelo script | Testar em 17 e entregar em 16 é a divergência silenciosa que o roadmap nomeou. O script roda quando alguém lembra; a suíte roda sempre |
| Embarcar o 17 | O pgvector do conda-forge win-64 exige `libpq <17.0a0` (Apêndice A, passo 2) |
| Compilar o pgvector para o 17 no Windows | Traria toolchain MSVC e PGXS para o build do projeto, que é o custo que o RFC-002 §1 evitou ao escolher uma imagem pronta |
| `pg_upgrade` do volume 17 para o 16 | O `pg_upgrade` não desce de versão |
| Dump completo, com esquema e dados | Carrega no 16 uma DDL escrita pelo 17 e deixa as migrations sem prova no 16 (§7) |
| Recriar o dev e reindexar | Exige a pasta real acessível e perde a linha do dispositivo, o job e as posições do `exif_backfill` (§2.4) |
| Apagar o volume do 17 na mesma etapa | Elimina o único caminho de volta antes de a validação dizer que ele não é necessário (§7) |
| Recuperar o script original | Ele nunca foi versionado, então não há objeto para recuperar. E a reescrita teria de acrescentar o que o log omitia de qualquer forma (§2.2) |
| Versionar o `.log` com `git add -f` | Abriria uma exceção numa convenção que todo experimento desde o RFC-030 segue. A transcrição no Apêndice A preserva o conteúdo sem exceção |
| ICU como provedor de localidade | A ordem depende da versão do ICU, e atualizar o instalador com outro ICU exige `REINDEX` (§5) |
| Localidade linguística da libc nos dois | Não existe uma com o mesmo nome e o mesmo comportamento no glibc e no MSVC (§5) |
| Confiar na suíte como prova de paridade | Ela passou 1616 vezes num cluster de localidade desconhecida (§2.3, §6) |
| Rodar a suíte com o Docker ligado sem verificar o alvo | Um nome de variável errado faz a suíte passar contra o Docker e imprimir o mesmo `passed` (§4.2) |

## 10. Não-objetivos

- **O adaptador de produção** (RFC-033): binários localizados pela instalação, `pgdata` em `%LOCALAPPDATA%`, senha guardada, porta alternativa e recuperação de encerramento sujo.
- **Um `Settings` que funcione sem checkout do repositório** (RFC-034).
- **Tirar o Docker do dia a dia do desenvolvimento.** O Compose continua até o launcher do RFC-036 oferecer o mesmo comando.
- **Um banco de teste dedicado.** A ressalva de `empty_db_session`, em `tests/conftest.py`, continua valendo.
- **Corrigir o `lower()` do `device_reconcile` para caracteres não ASCII** (§5).
- **CI.**
- **Atualizar o `README.md` da raiz**, que ainda descreve o projeto como *"containerização completa"*. É um documento de uso, e muda quando o RFC-036 mudar o comando de subida.

## 11. Riscos

| risco | situação |
| --- | --- |
| A suíte quebra no dev com a localidade `C` | Indicaria um teste que depende de ordem linguística ou de `lower()` fora do ASCII. É um achado desta etapa, não ruído, porque o embarcado também roda em `C`. A resposta é corrigir o teste ou rever §5, com o teste em mãos |
| As builds fixadas saem do conda-forge | O passo 2 falha de forma explícita. Trocar de build é outra medição, e o log registra qual foi usada |
| O `pg_dump` 17 escreve, além do `SET transaction_timeout`, algo que o 16 recusa | O `ON_ERROR_STOP` aborta a carga, e o volume do 17 continua intacto (§7) |
| Os números do script diferem dos do spike | É esperado: outro head, outra suíte, talvez outra máquina. Os dois ficam registrados lado a lado no ADR-008, e a linha de base do RFC-033 passa a ser a do script, não a do spike |
| A tag `0.8.6-pg16` congelou uma versão menor antiga do 16 | Divergência aceita (§6.1), registrada pela tabela de paridade |
| O `--initdb-defaults` mostra que o padrão nesta máquina não é `WIN1252` | O argumento de §5 para `UTF8` continua de pé. O defeito é um padrão que depende da localidade da máquina do usuário, qualquer que seja o valor nesta |

## 12. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-033a-linha-de-base-do-postgres-embarcado.md` | Este documento |
| `docs/adr/adr-008-postgresql-embarcado.md` | §8 |
| `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py` (+ `.log`, fora do git) | §4 e §6 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `docker-compose.yml` | Tag fixa `0.8.6-pg16`, `POSTGRES_INITDB_ARGS` e volume `postgres_data_pg16` |
| `docs/adr/README.md` | Linha do ADR-008 |
| `docs/adr/adr-002-postgresql-em-vez-de-banco-vetorial.md` | Nota de revisão na linha sobre deploy |
| `docs/rfcs/rfc-002-docker-e-postgres.md` | Nota: a imagem passou a `0.8.6-pg16` por este RFC. O texto original fica |
| `docs/rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md` | Nota em §14 que aponta para o ADR-008 |
| `AI_Context.md` | A frase *"with only PostgreSQL in a container"* |
| `docs/rfcs/ROADMAP-standalone.md` | O link do log passa a apontar para o Apêndice A, e a Etapa 0 é marcada como feita |
| `docs/rfcs/README.md` | A linha deste RFC |

Nenhum arquivo sob `backend/` muda.

## 13. Validação

Medido em 2026-10-08, na mesma máquina do spike: Intel Family 6 Model 158, 4 núcleos, Windows 10, Python 3.12.9, disco único `ST1000DM010-2EP102` (HDD SATA).

| verificação | como | resultado |
| --- | --- | --- |
| O script reproduz o spike | Docker desligado, de ponta a ponta; cada passo do Apêndice A aparece no log novo | **Sim.** `measure_embedded_postgres.log` (22:32, `docker: installed but NOT running`, exit 0) traz os passos 1, 2, 3, 4, 5, 6, 7a e 7b do Apêndice A, na mesma ordem e com os mesmos títulos, mais o 4b (`--initdb-defaults`) e o 8. As builds são as mesmas, a restrição `pgvector → libpq` é a mesma, a distância é a mesma (`0.2857142857142857`) e o `version()` também: `PostgreSQL 16.15, compiled by Visual C++ build 1944, 64-bit`. O `DeprecationWarning` sumiu. O ambiente explícito tem 16 pacotes |
| A suíte falou com o cluster embarcado | Porta e versão lidas pelo `settings` do projeto; `xact_commit` subiu | **Sim, nas duas execuções.** O subprocesso leu `127.0.0.1:51336/solidvision` pelo `settings`. O servidor respondeu `inet_server_port() = 51336` e `16.15 … Visual C++`, e o `xact_commit` foi de 18 a 338 (+320). Com o Docker ligado (`--parity`), na porta 55750, também foram +320 |
| Head e esquema do RFC-032 no embarcado | `6d77379a36a1`; três colunas e três `CHECK`s presentes | **Sim.** Head `6d77379a36a1`; colunas `latitude`, `longitude` e `position_source`; `CHECK`s `ck_images_latitude_range`, `ck_images_longitude_range` e `ck_images_position_pairing`; índice `ix_images_embedding_hnsw` com `vector_cosine_ops` |
| Números da nova linha de base | `initdb`, payload, `listening → ready`, Alembic, suíte | **24,7** s, **123** MB, **0,216** s, **2,5** s, **2085** passed (58 deselected, suíte em 70,4 s). Com o Docker ligado: 29,3 s, 123 MB, 0,235 s, 2,4 s, 2085 passed. O `initdb` variou de 24,7 a 38,9 s entre as três execuções completas, e a causa não foi medida. O spike tinha 27,2 s, 110 MB, 0,248 s, 3,3 s e 1616 passed (§14 explica o payload) |
| O script não deixa processo para trás | Nenhum `postgres.exe` na lista de processos ao final, inclusive com um passo forçado a falhar | **Nenhum**, ao fim das três execuções completas. Com `--fail-at 6`, o cluster subiu no passo 5, o passo 6 falhou, o `finally` parou o cluster e o script saiu com 1, sem processo para trás. Conferido também por fora, com `Get-Process postgres`: 0 |
| Os dados do dev sobrevivem | Contagens e `md5` antes e depois (§7) | **Idênticos** no 17.10, antes, e no 16.15, depois. São 60 imagens, todas com embedding e 40 com posição, 1 dispositivo, 1 job e 1 escopo. Head `6d77379a36a1`. `md5` dos embeddings `67cecd11d55b5ee3c6d2d24e966c7fc4`, o mesmo do RFC-032 §13. `md5` das posições `ca0b432d9f30c6afd84b381bd368fc7c`. O `md5` de todas as linhas das quatro tabelas também bateu (acréscimo, §14). Nada mudou depois das duas suítes |
| Paridade | `--parity`: todas as linhas iguais, exceto as de §6.1 | **Todas iguais.** A única linha marcada como registrada (§6.1) é a versão menor, e ela também é igual: 16.15 nos dois. A ordenação e o `lower()` da lista fixa dão a mesma sequência byte a byte, e `Łódź/東京/✈.jpg` volta intacto dos dois lados. Antes da troca, a mesma tabela apontava cinco defeitos contra o dev 17: versão maior, pgvector 0.8.5, localidade `en_US.utf8`, ordenação e `lower()` |
| A suíte passa nos dois | Mesma contagem de passed e deselected no dev 16 e no embarcado; `pytest -m slow` no dev | **2085 passed, 58 deselected** no dev 16 e no embarcado. `pytest -m slow` no dev 16 deu **58 passed**, com o CLIP real. O risco de §11 (a suíte quebrar em `C`) não se materializou |
| Estilo do script | `black --check` e `ruff check` | Limpos |
| O que se perdeu não se perde do mesmo jeito | `git ls-files experiments/rfc-033-embedded-postgres/` lista o `.py` | **Sim.** Lista só `measure_embedded_postgres.py`, no mesmo commit que este documento. Os três `.log` (spike, linha de base e paridade) aparecem em `git status --ignored` como ignorados por `*.log`, como deviam |
| O caminho de volta continua disponível | `solidvision_postgres_data` ainda existe ao fim | **Sim**, ao lado de `solidvision_postgres_data_pg16` |

## 14. Correções feitas durante a implementação

Registradas à vista do texto original, como o RFC-032 fez com as suas. Nenhuma muda uma decisão de §3.

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §5, candidato ICU | *"O recipe do conda-forge declara `icu` para todas as plataformas"* | O ICU 78.3 está no ambiente, mas só como dependência do `libxml2` e do `libxml2-16`. O servidor foi compilado **sem** ICU: `CREATE COLLATION … (provider = icu)` responde *"ICU is not supported in this build"*, como o passo 8 imprime. Do lado embarcado, o ICU nem é candidato, e a escolha por `C` fica mais forte. As DLLs do ICU somam 47 MB do payload sem servir ao banco. O ADR-008 registra isso como custo |
| §2.1, Apêndice A, passo 3 | Payload de 110 MB: 308 MB menos 184 MB de `.pdb` | A conta do log não fecha, porque 308 − 184 = 124. Removendo só os `.pdb` (154 arquivos, 184 MB), como o log diz, o resultado é **123 MB**. Os ~13 MB de diferença batem com `Library/include` (12,4 MB) mais `conda-meta` (1,2 MB), o que sugere que o spike removeu mais do que registrou. O script remove só o que declara e imprime a composição do payload. A linha de base passa a ser 123 MB |
| Apêndice A, condições | *"storage: system SSD, warm"* | A máquina tem um único disco, e ele é mecânico: `ST1000DM010-2EP102`, HDD SATA. A linha era uma constante no script do spike. O script novo lê o meio de armazenamento com `Get-PhysicalDisk` |
| §6.1 | A tag `0.8.6-pg16` congelou numa 16.x anterior | Congelou na **16.15**, a mesma do embarcado. A linha continua marcada como registrada, e não exigida |
| §7, passo 6 | Remover o `SET transaction_timeout` (a confirmar) | Confirmado pela tentativa. Com a linha, o 16 aborta com rc=3 e zero linhas carregadas. A linha 13 foi a única removida. A carga também usou `--single-transaction`, para que um erro no meio não deixasse metade dos dados. O `psql` 16.15 aceitou o `\restrict` que o `pg_dump` 17.10 escreve |
| §7, passos 1 e 7 | Contagens, `md5` dos embeddings e das posições, head | Feito como escrito, mais o `md5` de todas as colunas de todas as linhas de `devices`, `images`, `indexing_jobs` e `indexing_job_scopes`, porque o dump leva mais do que embeddings e posições. A própria conferência mostrou a colação nova: o `ORDER BY table_name` dela pôs `indexing_jobs` antes de `indexing_job_scopes` no 17 (`en_US.utf8` ignora o `_` no primeiro nível) e depois dele no 16 (`_` vem antes de `s` em bytes) |
| §4.1, passo 1 | Baixar o binário avulso | O log não registra de onde o spike baixou. O script baixa o pacote do conda-forge (`micromamba-2.9.0-0.tar.bz2`), confere o `sha256` contra a listagem do próprio conda-forge e extrai só o `micromamba.exe` |
| §4.1, passo 4 | A linha em que o `initdb` anuncia localidade e encoding vai para o log | O `initdb` sempre anuncia a localidade, mas só anuncia o encoding quando o escolhe sozinho. Com `--encoding=UTF8` explícito, essa linha não aparece, e o encoding é lido do servidor no passo 8 |
| §4.2 | `xact_commit` *"precisa ter subido"* | Precisa subir mais de 10. As leituras do próprio script fazem poucas transações, e a suíte faz centenas (+320 em cada execução) |
| §4.3 | Parar o cluster e conferir a lista de processos | A saída do `pg_ctl` vai para um arquivo, e não para um pipe. Um pipe herdado pelo servidor faria o `subprocess.run` esperar por um processo que não termina. Foi uma precaução, não um travamento observado, mas o launcher do RFC-036 vai lidar com a mesma herança |
| §13, passo forçado a falhar | — | O script ganhou `--fail-at PASSO`, que faz o passo falhar logo ao começar |
| Apêndice A | O log não é versionado | O arquivo original foi preservado como `measure_embedded_postgres.spike-2026-09-22.log`, também fora do git, porque a execução nova escreve `measure_embedded_postgres.log` |
| §2.3 | Suíte padrão do dev: 2047 passed (RFC-032 §13) | Hoje são 2085 passed, porque os commits do CLI posteriores ao RFC-032 acrescentaram testes. As comparações de §13 usam a mesma árvore nos dois bancos |

---

## Apêndice A — O log do spike, transcrito

O arquivo `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.log` não é versionado (§2.1). Esta é a transcrição dele, feita em 2026-10-08. Os caminhos foram encurtados para `<repo>` (a raiz do repositório) e `<scratch>` (o diretório temporário da sessão em que o spike rodou). Nada mais foi alterado.

```text
<repo>\experiments\rfc-033-embedded-postgres\measure_embedded_postgres.py:173: DeprecationWarning: Python 3.14 will, by default, filter extracted tar archives and reject files or modify their metadata. Use the filter argument to control this behavior.
  tar.extract(member, work)
========================================================================
RFC-033 spike: PostgreSQL + pgvector on Windows, without Docker
========================================================================
when:     2026-09-22 09:37:06 -0300
work dir: <scratch>\pgspike3
repo:     <repo>

conditions
    platform:   win32, AMD64
    python:     3.12.9
    cpu:        Intel64 Family 6 Model 158 Stepping 9, GenuineIntel x4
    docker:     installed but NOT running
    storage:    system SSD, warm -- no cold mechanical disk below

--- 1. fetch micromamba (standalone, not installed)
    11 MB -> <scratch>\pgspike3\micromamba.exe
    2.9.0
    [4.68 s] 1. fetch micromamba (standalone, not installed)

--- 2. materialize postgresql=16.15 + pgvector=0.8.6 (win-64)
    libpq              16.15-h43e12c5_0
    pgvector           0.8.6-h2466b09_0
    pgvector->libpq    libpq >=16.15,<16.16.0a0, libpq >=16.15,<17.0a0
    postgresql         16.15-he837cf3_0
    [19.80 s] 2. materialize postgresql=16.15 + pgvector=0.8.6 (win-64)
    OK postgres.exe
    OK initdb.exe
    OK pg_ctl.exe
    OK psql.exe
    OK lib/vector.dll

--- 3. prune what an installer would not ship
    before: 308 MB
    debug symbols removed: 184 MB
    after:  110 MB   <- the installer payload
    [3.03 s] 3. prune what an installer would not ship

--- 4. initdb (scram auth, password file)
    rc=0  cluster: 38 MB
    [27.21 s] 4. initdb (scram auth, password file)

--- 5. start on loopback, high port
    listening -> ready: 0.248 s  (from the server's own log)
    PostgreSQL 16.15, compiled by Visual C++ build 1944, 64-bit
    [1.26 s] 5. start on loopback, high port

--- 6. CREATE EXTENSION vector, then the RFC-018 index
    CREATE EXTENSION rc=0 
    installed: vector 0.8.6
    '<=>' cosine distance: 0.2857142857142857
    HNSW vector_cosine_ops rc=0 
    [1.67 s] 6. CREATE EXTENSION vector, then the RFC-018 index

--- 7a. alembic upgrade head
    rc=0
    head:    f4b9e2d7c615
    tables:  alembic_version, devices, images, indexing_job_scopes, indexing_jobs
    index:   ix_images_embedding_hnsw
    [3.32 s] 7a. alembic upgrade head

--- 7b. full default pytest suite against this cluster
    rc=0
    1616 passed, 58 deselected, 34 warnings in 72.30s (0:01:12)
    [76.74 s] 7b. full default pytest suite against this cluster

========================================================================
VERDICT
========================================================================
  initdb (once, per machine):   27.2 s
  installer payload (pruned):   110 MB
  suite:                        PASSED
  Docker:                       not running, not required
  cluster stopped.
```
