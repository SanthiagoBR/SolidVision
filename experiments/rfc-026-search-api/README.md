# RFC-026 Search API -- verificações para um amigo

Dois scripts, dois objetivos diferentes. Ambos são autocontidos: clone o
repositório, siga a configuração compartilhada abaixo, rode um comando e
leia o resultado.

| script | escreve no Postgres? | responde |
| --- | --- | --- |
| `friend_e2e_check.py` | Não -- indexa dentro de uma transação que é desfeita (rollback) no final | *Uma requisição HTTP real chega ao CLIP real e ao PostgreSQL real, e volta com o JSON que a RFC-026 promete?* PASS/FAIL, 16 verificações. |
| `friend_indexing_timing_check.py` | **Sim** -- um upsert real, que permanece no banco | *Quanto tempo leva para indexar por imagem, e para buscar por consulta -- com a tradução medida separadamente do resto da busca?* Números reais, mais uma checagem de sanidade do recall. |

Se você só quer os números, vá direto ao script 2. Se quiser um sinal de
"funciona ou não" de que todo o caminho HTTP está de pé antes de confiar
em qualquer número, rode o script 1 primeiro.

## Configuração (compartilhada pelos dois scripts)

Escrito do zero, como se você nunca tivesse tocado neste repositório --
siga os passos na ordem, sem pular nenhum.

**Pré-requisitos:** Python 3.12+, Docker com Docker Compose, e Git
instalados na máquina.

### 1. Clonar o repositório e entrar nele

```bash
git clone https://github.com/SanthiagoBR/SolidVision.git
cd SolidVision
git checkout bakeoff/rfc-026-search-api
```

`git clone` sempre baixa todos os branches remotos, então
`git checkout bakeoff/rfc-026-search-api` já funciona direto depois do
clone -- não precisa de `git fetch` nem de nenhum passo intermediário.

Todos os comandos abaixo assumem que você está na raiz do repositório
(a pasta `SolidVision/` que acabou de entrar), a menos que um `cd`
explícito diga o contrário.

### 2. Criar o ambiente virtual e instalar as dependências

```bash
python -m venv backend/.venv
```

Ative o ambiente (`backend\.venv\Scripts\Activate.ps1` no Windows,
`source backend/.venv/bin/activate` no Bash) e então:

```bash
pip install -r backend/requirements.txt
```

### 3. Criar o arquivo `.env` -- e corrigir um campo nele

```bash
cp .env.example .env
```

(No Windows sem Bash: `copy .env.example .env`.) Isso precisa acontecer
**antes** do próximo passo -- o `docker-compose.yml` lê `DATABASE_NAME`,
`DATABASE_USER` e `DATABASE_PASSWORD` direto do `.env`, e sem ele o
Postgres sobe com essas variáveis vazias.

**Agora abra o `.env` recém-criado e troque uma linha.**
`.env.example` vem com `DATABASE_HOST=postgres` -- esse valor só
funciona para um *outro container* rodando na mesma rede Docker do
Postgres, resolvendo `postgres` como hostname via a DNS interna do
Docker. Os dois scripts deste diretório rodam direto na sua máquina
(no host), não dentro de um container, e o `docker-compose.yml` expõe a
porta do Postgres em `localhost`, não em `postgres`. Sem essa troca, a
conexão com o banco falha logo no primeiro passo de qualquer um dos
dois scripts. Troque:

```diff
-DATABASE_HOST=postgres
+DATABASE_HOST=localhost
```

Depois da troca, o bloco de banco de dados do seu `.env` deve ficar:

```text
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=solidvision
DATABASE_USER=solidvision
DATABASE_PASSWORD=solidvision
```

O resto do `.env.example` -- modelo, dimensão do embedding,
`WARM_UP_MODELS` etc. -- já vem com valores que funcionam sem ajuste.

### 4. Subir o PostgreSQL e aplicar as migrations

```bash
docker compose up -d
cd backend
alembic upgrade head
alembic heads
cd ..
```

`alembic heads` deve imprimir exatamente `26058b9e1d9a (head)` -- a
RFC-026 não adiciona nenhuma migration, então esse número é o mesmo que
você já veria na `develop`. O `cd backend` é necessário (o `alembic.ini`
resolve `script_location` a partir do diretório atual, não a partir de
onde o `.ini` está); o `cd ..` no final devolve você à raiz do
repositório, de onde os dois scripts abaixo são chamados.

**A primeira execução de qualquer um dos scripts baixa dois checkpoints
do Hugging Face** -- o CLIP (~5 s para carregar depois de baixado) e, na
primeira vez que uma string em português precisar ser traduzida, o
Marian (~5 s a mais). Isso é esperado numa execução "fria", não é um
travamento; toda execução seguinte é rápida porque ambos ficam em cache
em disco.

---

## Script 1 -- `friend_e2e_check.py`: o caminho HTTP funciona de ponta a ponta?

```bash
python experiments/rfc-026-search-api/friend_e2e_check.py
```

Indexa o corpus de demonstração de 45 imagens **dentro de uma transação
que é desfeita no final** -- nada que ele escreve sobrevive --, sobe a
aplicação FastAPI real sob um servidor `uvicorn` real, e dispara
requisições HTTP reais contra `/health` e `/api/v1/images/search`: uma
consulta em inglês, uma em português (verificando que a tradução
realmente rodou via HTTP), e os três casos de erro documentados. Imprime
uma linha `[PASS]`/`[FAIL]` por verificação, 16 no total, e encerra com
código `1` se algo falhar.

### Como é um PASS

```text
======================================================================
RFC-026 Search API -- end-to-end check
======================================================================

1. Checking the database connection...
  [PASS] PostgreSQL is reachable
  ...
7. Checking the documented error cases...
  [PASS] blank query -> 400
  [PASS] limit=101 -> 400
  [PASS] missing q -> 422

======================================================================
ALL 16 CHECKS PASSED
======================================================================
```

(A saída acima é exatamente o que o script imprime -- em inglês --,
reproduzida aqui sem tradução para que você reconheça o texto real na
sua tela.)

---

## Script 2 -- `friend_indexing_timing_check.py`: quão rápido, de verdade?

```bash
python experiments/rfc-026-search-api/friend_indexing_timing_check.py
```

**Este aqui escreve de verdade.** Ele indexa as 45 imagens do corpus de
demonstração diretamente de `backend/dataset/demo/` no seu banco de
dados real -- um upsert, então rodar o script de novo é seguro e apenas
atualiza as mesmas 45 linhas -- e as deixa lá. Depois de rodar, você
tem um corpus real, pesquisável e navegável via `psql`, não um corpus
que desaparece quando o script termina.

Três coisas são medidas e impressas por imagem ou por consulta, em vez
de só como um agregado:

1. **Indexação** -- uma linha por imagem, dividida entre o encode do
   CLIP e a escrita no PostgreSQL (a RFC-024 mediu que são custos bem
   diferentes: o encode domina, a persistência é ~1-2% de uma execução).
2. **Busca em inglês** -- as 25 consultas de referência (ground truth)
   da RFC-022, cada uma cronometrada, cada uma checada quanto a se a
   imagem esperada aparece entre os top 5 resultados.
3. **Busca em português, com a tradução cronometrada separadamente** --
   as mesmas 25 consultas, traduzidas à mão (as mesmas frases do
   `PORTUGUESE_TRANSLATIONS` do bake-off do SigLIP da RFC-023, para que
   esses números sejam comparáveis àquela execução). `build_prompt()`
   -- exatamente `detectar -> traduzir -> aplicar o template`, nada
   além disso -- é cronometrado sozinho primeiro, e depois a busca
   completa é cronometrada em separado. A busca completa
   necessariamente roda a tradução de novo internamente (não existe uma
   API pública para passar ao `encode_text` um prompt já pronto); esse
   passo extra custa a um script de benchmark algumas centenas de
   milissegundos e compra, em troca, um número honesto de "só a
   tradução", em vez de um número estimado por subtração.

### Como é uma execução

```text
======================================================================
RFC-026 Search API -- indexing + query timing check
======================================================================
...
4. Indexing all 45 images into PostgreSQL...

  [ 1/45] cleared_lot_01               encode   612.3 ms   persist    78.1 ms
  [ 2/45] cleared_lot_02               encode   598.7 ms   persist    12.4 ms
  ...

  Indexed 45/45, 0 failed.
  encode time / image         mean   601.2 ms   median   595.0 ms   min   580.1 ms   max   720.4 ms
  persist time / image        mean    14.3 ms   median    11.9 ms   min     9.2 ms   max    79.1 ms
  total indexing wall time: 27.68 s

5. Running the 25 English ground-truth queries...

  [hit ]    91.2 ms  top1_sim=+0.3737  artificial fish farming ponds in a valley
  ...

  EN Recall@5: 84.0% (21/25)
  EN search time / query      mean    93.4 ms   median    91.0 ms   min    85.2 ms   max   128.9 ms

6. Running the same 25 queries in Portuguese, translation timed separately...

  [hit ] translate   287.4 ms  full-search   382.1 ms  'propriedade rural com um pequeno lago' -> 'a photo of . a rural property with a small lake'
  ...

  PT Recall@5: 80.0% (20/25)
  translation time / query    mean   280.6 ms   median   275.3 ms   min   260.1 ms   max   340.7 ms
  PT full search time / query mean   378.9 ms   median   373.5 ms   min   350.2 ms   max   455.8 ms

======================================================================
PASS -- all 45 images indexed, both recall floors met (>= 76%)
======================================================================
```

(De novo, essa é a saída literal do script -- em inglês -- reproduzida
sem tradução.)

Os números acima são ilustrativos, não uma garantia -- os seus vão
variar de acordo com sua CPU/GPU, e o recall em particular deve se
mover em uma ou duas consultas entre execuções (a RFC-025 seção 11
mediu um checkpoint que atinge 64% de strict top-1 nesse corpus; o
ruído é do modelo, não do pipeline). O script encerra com código `0`
somente se as 45 imagens foram indexadas e **ambos** os Recall@5 --
inglês e português -- ficarem em 76% ou acima -- o mesmo limite que
`tests/dataset/test_semantic_search_e2e.py` usa como critério de
aprovação. Ficar abaixo desse limite vale a pena investigar; uma ou
duas consultas abaixo de 100% não.

---

## Solução de problemas

| sintoma | causa provável |
| --- | --- |
| `Could not reach PostgreSQL` | Causa mais comum: `.env` ainda tem `DATABASE_HOST=postgres` (o valor de `.env.example`) em vez de `localhost` -- veja o passo 3. Se já estiver como `localhost`, confira se `docker compose up -d` está rodando com `docker compose ps` |
| Trava por muito tempo em "Loading CLIP" | Download da primeira execução; precisa de acesso à internet para huggingface.co. Observe se aparece uma barra de progresso -- se realmente não houver nenhuma depois de um ou dois minutos, verifique sua rede |
| Script 1: menos de 45 imagens indexadas | Verifique se `backend/dataset/demo/` está presente e intacto -- `manifest.verify_matches_directory()` falha alto e claro se um arquivo foi movido ou alterado |
| Script 1: `The server started` falha | A porta 8127 já está em uso por outra coisa nesta máquina |
| Script 2: `WARNING: no PT translation for N quer(ies)` | Uma consulta em `queries.json` não bate, palavra por palavra, com nenhuma chave em `PORTUGUESE_TRANSLATIONS` deste script -- o ground truth mudou desde que esse dicionário foi copiado |
| Script 2 com recall abaixo de 76% nos dois idiomas | Vale a pena olhar com mais calma -- rode de novo uma vez (a ordem de carregamento dos modelos pode importar numa GPU "fria") antes de assumir que algo está quebrado |
| Tudo falha de uma vez com um traceback | Leia o traceback -- ele é impresso por completo, não é engolido |

## O que estes scripts não são

Nenhum dos dois substitui `pytest -m slow`, que é o critério de
aprovação de verdade: `tests/dataset/test_semantic_search_e2e.py`
calcula Recall@5, contaminação por hard negatives, strict top-1 e
acurácia pairwise sobre o mesmo corpus de 45 imagens com limites
medidos, e `backend/tests/presentation/test_search_api_e2e.py` prova a
mesma pilha de novo, via HTTP real. Esses dois scripts existem para que
um amigo consiga uma resposta rápida e legível -- *funciona, e quão
rápido* -- sem precisar ler nenhum dos dois arquivos de teste nem
lembrar um comando `pytest -m` de cor. O script 1 prova o transporte; o
script 2 prova que ele pode ser confiado com dados reais e persistentes,
e coloca números nisso. Nenhum dos dois recalcula do zero os limites de
qualidade da RFC-025 -- ambos reaproveitam o número de 76% de Recall@5
que aquele teste já mediu e usa como critério, então o laptop de um
amigo e o CI são cobrados pela mesma régua. Se você quiser todo o
detalhe por trás de cada decisão que qualquer um dos scripts verifica,
`docs/rfcs/rfc-026-search-api.md` é onde isso é discutido a fundo.
