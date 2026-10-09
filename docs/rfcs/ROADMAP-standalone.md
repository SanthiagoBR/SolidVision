# Roadmap — SolidVision sem Docker

Este roadmap cobre o trabalho entre o backend atual (Sprint 6, RFC-032) e o **início do frontend**. Ele acaba quando uma máquina Windows com o Docker desligado sobe o SolidVision inteiro com um único comando e funciona offline. O instalador, a interface e o empacotamento do runtime ficam de fora (ver [Fora deste roadmap](#fora-deste-roadmap)).

**Por que antes do frontend.** O risco está no Postgres embarcado, não nas telas. O frontend é trabalho previsível. Já o ciclo de vida de um banco dentro de um app desktop (primeira execução, porta ocupada, encerramento sujo, versão fixada do pgvector) é onde as surpresas aparecem. Além disso, o processo que supervisiona Postgres, uvicorn e `job_runner` decide como o frontend vai ser servido e aberto. O frontend depende dessa decisão, e não o contrário.

Status segue as [convenções](README.md#convenções): todo número é `TBM` até a implementação escrevê-lo de volta.

---

## Ponto de partida

O que já foi medido no spike de 2026-09-22 ([log transcrito no RFC-033a, Apêndice A](rfc-033a-linha-de-base-do-postgres-embarcado.md#apêndice-a--o-log-do-spike-transcrito)), e o que o script reescrito mediu em 2026-10-08 ([RFC-033a §13](rfc-033a-linha-de-base-do-postgres-embarcado.md#13-validação)). **A linha de base do RFC-033 é a segunda coluna.**

| Medição | Spike (head `f4b9e2d7c615`) | Linha de base (head `6d77379a36a1`) |
|---|---|---|
| Binários | PostgreSQL 16.15 + pgvector 0.8.6, conda-forge win-64 | Os mesmos, com as builds fixadas e o ambiente explícito no log |
| Payload depois de remover os `.pdb` | 110 MB (de 308 MB) | 123 MB (de 308 MB). A conta do spike não fechava ([§14](rfc-033a-linha-de-base-do-postgres-embarcado.md#14-correções-feitas-durante-a-implementação)) |
| `initdb`, uma vez por máquina | 27,2 s | 24,7 s, num HDD |
| Postgres de `listening` a `ready` | 0,248 s | 0,216 s |
| `alembic upgrade head` | 3,3 s | 2,5 s |
| Suíte padrão contra o cluster embarcado | 1616 passed, Docker desligado | 2085 passed, Docker desligado, alvo provado pela porta e por `xact_commit` |

O que **está faltando ou divergente** no repositório hoje:

- **O script do spike sumiu.** O log cita `experiments/rfc-033-embedded-postgres/measure_embedded_postgres.py`, que não existe no repositório nem no histórico do git. Só o log sobreviveu.
- **O ADR-008 não existe.** A decisão de embarcar o Postgres foi tomada, mas não está registrada em `docs/adr/`.
- **Dev e produção rodam versões maiores diferentes do Postgres.** O `docker-compose.yml` usa `pgvector/pgvector:pg17`, e o alvo embarcado é 16.15. O pgvector do conda-forge win-64 está preso à libpq 16.x, então o 16 não é negociável. Testar em 17 e entregar em 16 é uma divergência silenciosa.
- **O spike rodou contra o head `f4b9e2d7c615`.** A migration do RFC-032 (`6d77379a36a1_add_image_position`) veio depois e ainda não passou pelo Postgres embarcado.

> **Os quatro itens acima foram resolvidos pela Etapa 0** ([RFC-033a](rfc-033a-linha-de-base-do-postgres-embarcado.md)). Os dois abaixo são do RFC-034 e do RFC-035.

- **A configuração presume um checkout do repositório.** O `Settings` lê `.env` de `parents[4]`, ou seja, da raiz do repositório. `log_directory` é relativo ao diretório de trabalho (`"logs"`), e `indexing_root_path` tem um default relativo. Só `thumbnail_directory` já usa `%LOCALAPPDATA%\SolidVision`.
- **Os dois modelos são baixados do Hugging Face na primeira execução.** O CLIP e o tradutor PT→EN usam `from_pretrained(nome)`. Sem rede, a primeira busca falha.

---

## Etapa 0 — Reconstruir o que se perdeu ✅

Feita no [RFC-033a](rfc-033a-linha-de-base-do-postgres-embarcado.md) ✅. Duas entregas saíram diferentes da tabela abaixo, e o RFC diz por quê. A tag ficou fixa em `pgvector/pgvector:0.8.6-pg16`, porque `pg16` já traz o pgvector 0.8.7. E o ADR-008 separa os motivos que valem para o banco (Docker Desktop como pré-requisito, transporte medido, ciclo de vida) dos quatro que já decidiram onde a API roda.

| Entrega | Critério de pronto |
|---|---|
| `docs/adr/adr-008-postgresql-embarcado.md` | Registra a decisão, os motivos (identidade de volume do RFC-027, HDs externos plugados a quente, reveal do RFC-030, I/O atravessando a VM, Docker Desktop como exigência para um fotógrafo) e os números do spike |
| `measure_embedded_postgres.py` reescrito | Reproduz o log de ponta a ponta e roda até o head atual, incluindo a migration do RFC-032 |
| `docker-compose.yml` em `pgvector/pgvector:pg16` | Dev e produção na mesma versão maior; a suíte passa nos dois |

É pequeno, mas vem primeiro: sem o script, a Etapa 1 não tem como provar que não regrediu em relação ao spike.

---

## RFC-033 — PostgreSQL embarcado 📋

Transforma o spike em código de produção: um adaptador de infraestrutura que é dono do ciclo de vida do cluster.

Proposto no [RFC-033](rfc-033-postgres-embarcado.md) 📋. Três itens abaixo saíram diferentes no RFC, e ele diz por quê ([§2.3](rfc-033-postgres-embarcado.md#23-três-coisas-que-o-roadmap-presumiu)). O `CREATE EXTENSION` já está na migration `999b801e80f4`, então o que falta é o `CREATE DATABASE`. O diretório do cluster é `postgres\16\data`, com a versão maior no caminho, para caberem dois clusters lado a lado. E "nunca pelo `PATH`" passa a valer também para as DLLs.

**Escopo**
- Localizar os binários vendorizados (`postgres.exe`, `initdb.exe`, `pg_ctl.exe`, `vector.dll`) por um caminho relativo à instalação, nunca pelo `PATH`.
- Usar `%LOCALAPPDATA%\SolidVision\pgdata` como diretório do cluster, no mesmo padrão dos thumbnails.
- Na primeira execução: rodar `initdb` com autenticação scram, gerar uma senha aleatória e guardá-la com permissão restrita ao usuário.
- Escutar só em loopback, numa porta alta, com uma alternativa quando a porta estiver ocupada.
- Iniciar e parar com `pg_ctl`; rodar `CREATE EXTENSION vector` e `alembic upgrade head` automaticamente, de forma idempotente.
- Recuperar de encerramento sujo: `postmaster.pid` órfão, processo morto pelo Gerenciador de Tarefas, máquina desligada no meio de uma escrita.
- Testar caminhos com espaço e acento (`C:\Users\João Silva\...`).

**Critérios de pronto**
- Primeira execução numa máquina limpa: `TBM` s, com o `initdb` medido separado do resto.
- Execução a quente, do comando até o banco aceitar conexões: `TBM` s.
- A suíte inteira passa contra o cluster que o adaptador gerencia, com o Docker desligado.
- Matar o `postgres.exe` à força e reiniciar recupera sem intervenção manual. Isso foi testado, não presumido.

**Riscos**
- Antivírus e SmartScreen podem bloquear o `postgres.exe` fora de `Program Files`.
- Uma atualização futura do Postgres para a versão maior 17 exige `pg_upgrade`, que nunca foi medido. Fica fora deste roadmap, mas a escolha do diretório de dados precisa deixar espaço para dois clusters lado a lado.

---

## RFC-034 — Configuração e diretórios do usuário 📋

Faz o backend funcionar sem um checkout do repositório.

**Escopo**
- Separar duas origens de configuração. No desenvolvimento, continua o `.env` da raiz. Instalado, um arquivo em `%LOCALAPPDATA%\SolidVision\` com precedência documentada.
- Tornar `log_directory` absoluto e por usuário.
- Remover o default relativo de `indexing_root_path`. O RFC-029 já indexa por `device_id` e escopos, e um default relativo num app instalado aponta para lugar nenhum.
- Fazer as credenciais do banco virem do RFC-033, não de um campo fixo.
- Ler `environment` (`development` ou instalado) de um único lugar.

**Critérios de pronto**
- O backend sobe a partir de um diretório qualquer, sem `.env`, e grava tudo em `%LOCALAPPDATA%\SolidVision`.
- Nenhum caminho relativo ao diretório de trabalho sobra no `Settings`. Um teste verifica isso.

---

## RFC-035 — Modelos offline 📋

**Escopo**
- Carregar o CLIP (laion ViT-B/32, [RFC-023](rfc-023-adaptador-de-embedding-clip.md)) e o tradutor PT→EN de um diretório local, e não pelo nome no Hugging Face Hub.
- Ligar `HF_HUB_OFFLINE=1` no modo instalado, para que uma falha de rede nunca vire um download silencioso.
- Verificar a integridade dos pesos com checksum, falhando com uma mensagem clara em vez de um traceback do `transformers`.
- Garantir que o embedding produzido seja idêntico ao atual. Pesos trocados ou de outra revisão invalidam o acervo inteiro já indexado.

**Critérios de pronto**
- Com a rede desligada, indexar e buscar (por texto em PT, por texto em EN e por imagem) funcionam numa máquina que nunca acessou o Hugging Face.
- Tamanho dos pesos em disco: `TBM` MB. Esse número alimenta a decisão do instalador.
- Embeddings bit a bit iguais aos do carregamento atual, num conjunto fixo de imagens.

---

## RFC-036 — Launcher e supervisão de processos 📋

Um ponto de entrada único que sobe e derruba os três processos. O RFC-029 §6 separou o executor da API de propósito, e o launcher preserva essa separação.

**Escopo**
- Ordem de subida: Postgres (RFC-033), migrations, depois uvicorn e `job_runner` em paralelo.
- Health checks antes de declarar pronto: `pg_isready` e a rota de health do [RFC-008](rfc-008-health-api.md).
- Política de reinício quando um filho morre. O `job_runner` já tem heartbeat e retomada (RFC-029). O launcher só precisa reiniciá-lo, não reimplementar a recuperação.
- Encerramento ordenado: parar de aceitar jobs, cancelamento cooperativo, uvicorn, e por último o Postgres. Nenhum `postgres.exe` órfão.
- Garantir uma instância só por usuário. Abrir o app duas vezes reaproveita a instância que já está rodando.
- Usar o mesmo comando no desenvolvimento e na instalação, para o dia a dia sair do Docker antes do frontend existir.

**Critérios de pronto**
- Numa máquina com o Docker desligado, um único comando deixa a API respondendo em `localhost`, em `TBM` s a quente.
- Matar cada um dos três filhos, um de cada vez, resulta em recuperação. O tempo é medido.
- Fechar o launcher não deixa nenhum processo para trás. Isso é verificado pela lista de processos, não pelo log.
- Um job longo interrompido pelo encerramento retoma na próxima subida.

**Decisão em aberto, que o frontend herda:** o launcher roda como app de bandeja, como serviço do Windows ou como processo filho de uma janela (Tauri, Electron ou WebView2). Este RFC decide ou deixa a decisão explícita, porque é ela que define como o frontend vai ser aberto.

---

## Pronto para o frontend

O frontend começa quando, **numa máquina com o Docker desligado e sem rede**:

1. Um único comando sobe Postgres, API e `job_runner`.
2. Os dados ficam em `%LOCALAPPDATA%\SolidVision`, e nada depende do checkout do repositório.
3. A busca por texto e por imagem funciona.
4. Fechar e reabrir não perde estado, não deixa processos órfãos e retoma jobs.
5. A suíte passa contra o Postgres embarcado, na mesma versão maior do dev.

## Fora deste roadmap

Deixados de fora de propósito, porque dependem do frontend ou só valem a pena depois dele:

- **Congelar o runtime Python** (PyInstaller ou Python embutido) e o tamanho final do torch em CPU.
- **Instalador** (Inno Setup ou WiX), assinatura de código, desinstalação e política sobre os dados do usuário.
- **Atualizações:** migrations entre versões do app, e `pg_upgrade` entre versões maiores do Postgres.
- **GPU:** ainda não medido. É a maior alavanca de velocidade (inferência em CPU ≈ 450 ms por imagem domina tudo), mas não é pré-requisito para o app ser standalone.
- **Varredura a frio de HD externo mecânico:** ainda não medida.
