# RFC-033 — PostgreSQL Embarcado

**Status:** Proposto
**Etapa:** 1 do [roadmap sem Docker](ROADMAP-standalone.md#rfc-033--postgresql-embarcado-)
**Depende de:** RFC-033a (linha de base e script de medição), [ADR-008](../adr/adr-008-postgresql-embarcado.md) (a decisão de embarcar), RFC-004 (variáveis de ambiente acima do `.env`), RFC-007 (Alembic), RFC-018 (a migration que cria a extensão), RFC-029 §6 (os processos rodam no host), RFC-030 §7.1 (`%LOCALAPPDATA%\SolidVision`)
**Bloqueia:** RFC-034 (as credenciais do banco passam a vir daqui), RFC-036 (o launcher sobe o banco por este adaptador)
**Migration:** não. Nenhuma revisão nova. O `alembic/env.py` passa a aceitar uma conexão recebida de quem o chama (§7.4)
**Medição:** `experiments/rfc-033-embedded-postgres/measure_adapter.py` — `TBM`

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os números medidos que este documento cita vêm do RFC-033a (2026-10-08, HDD SATA `ST1000DM010-2EP102`, Docker desligado), e cada um diz de onde veio. Onde o texto afirma como o Windows ou o PostgreSQL se comportam sem ter medido, a frase diz que é hipótese, e §18 diz como ela vai ser testada.

---

## 1. Contexto

O RFC-033a reconstruiu a linha de base. O PostgreSQL 16.15 com pgvector 0.8.6, tirados do conda-forge `win-64`, sobe com o Docker desligado, aplica as onze migrations em 2,5 s e passa 2085 testes. O ADR-008 registrou a decisão de levar esse par dentro do instalador, como processo filho do app. O desenvolvimento já roda a mesma versão maior, o mesmo pgvector, o mesmo encoding e a mesma localidade.

O que existe hoje, porém, é um **script de medição**. Ele baixa os binários num diretório temporário, cria um cluster novo, mede, para e apaga tudo. Toda execução começa do zero e termina limpa. Um app desktop vive o caso contrário: o cluster é criado uma vez e sobe centenas de vezes, às vezes depois de o processo ter sido morto, às vezes com a porta tomada por outro programa, às vezes a partir de um processo que o instalador deixou elevado.

Este RFC transforma o spike em código de produção. Ele cria um adaptador de infraestrutura que é **dono do ciclo de vida do cluster**: localiza os binários, cria o cluster na primeira execução, sobe ou adota um cluster que já está de pé, escolhe a porta, recupera de encerramento sujo, prepara o banco e para. Não decide quem o chama, que é o RFC-036, nem como o `Settings` funciona fora de um checkout, que é o RFC-034.

## 2. Problema

### 2.1 O spike provou que o banco sobe uma vez

O RFC-033a §4.4 deixou de fora, de propósito, tudo o que só acontece da segunda subida em diante: *"subida a quente sob um adaptador, recuperação de encerramento sujo, porta ocupada, antivírus e caminhos com acento no diretório de dados são critérios do RFC-033."* Nenhum deles foi exercitado até hoje.

O script do RFC-033a também faz escolhas que servem para medir e não servem para o produto:

| o script do RFC-033a | por que serve para medir | por que não serve para o produto |
| --- | --- | --- |
| Pede uma porta livre ao sistema a cada execução | O cluster vive alguns minutos | Quem precisa do banco tem de descobrir a porta, e ninguém sabe qual foi (§2.2) |
| Gera uma senha nova a cada execução | O cluster é apagado no fim | A senha precisa sobreviver ao processo que a gerou |
| Cria o cluster num diretório temporário | Nada deve sobrar | O acervo indexado vive ali, e custou horas de inferência |
| Põe a raiz do ambiente, `Library\bin` **e o `PATH` do usuário** no `PATH` dos filhos | Basta que o servidor suba | Esconde quais DLLs o servidor de fato precisa (§2.3) |
| Lista processos pelo PowerShell (`Get-CimInstance`) | Roda uma vez, no fim | Abrir um PowerShell custa segundos, e a subida a quente inteira deveria custar menos que isso |
| Executa SQL pelo `psql.exe` e o Alembic num subprocesso | Imita o que um operador faria | Cada subprocesso é um Python ou um executável a mais em toda subida |

### 2.2 Ninguém sabe a porta e a senha antes de o banco subir

A engine do projeto é um singleton de módulo. O `engine.py` a constrói **na importação**, a partir de `settings.database_url`, e o `Settings` lê o `.env` da raiz do repositório, onde estão a porta 5432 do Docker e uma senha fixa. Com o banco embarcado, a porta é escolhida na subida (§8.2) e a senha é gerada na primeira execução (§7.2). Quem sobe o cluster conhece as duas. Mais ninguém conhece.

O RFC-033a §4.2 já mostrou a ponte que funciona sem mudar código: as variáveis `DATABASE_*` no ambiente ficam acima do `.env` (RFC-004). Ele mostrou também a armadilha dessa ponte: uma variável com o nome errado faz o `Settings` cair no `.env`, o `.env` aponta para o Docker, e a suíte passa contra o banco errado imprimindo o mesmo `passed`. O RFC-034 é quem vai fazer as credenciais virem do adaptador. Até lá, a ponte são as variáveis, e quem as monta é o adaptador, não uma pessoa (§11).

### 2.3 Três coisas que o roadmap presumiu

**"Rodar `CREATE EXTENSION vector` e `alembic upgrade head`" é uma coisa só.** A migration `999b801e80f4` (RFC-018) já executa `CREATE EXTENSION IF NOT EXISTS vector`. Um passo separado seria um segundo lugar para manter em sincronia com a cadeia de migrations. O que falta de fato é o `CREATE DATABASE`. O `initdb` cria só `postgres`, `template0` e `template1`, e o script do RFC-033a criava o `solidvision` por conta própria, no passo 5.

**`%LOCALAPPDATA%\SolidVision\pgdata` contradiz o risco que o próprio roadmap registra.** O roadmap pede que *"a escolha do diretório de dados [deixe] espaço para dois clusters lado a lado"*, e o ADR-008 repete isso como trade-off aceito. O volume de desenvolvimento já leva a versão maior no nome (`postgres_data_pg16`). O diretório do cluster segue a mesma convenção: `postgres\16\data` (§6).

**"Nunca pelo `PATH`" precisa valer também para as DLLs.** O passo 3 do log do RFC-033a mede 5,0 MB de arquivos soltos na raiz do ambiente (*"(top-level files) 5.0"*). O ambiente explícito traz `vc14_runtime`, `vcomp14` e `ucrt`, que formam o runtime do Visual C++, e uma instalação limpa do Windows pode não tê-lo. Como o script pôs a raiz do ambiente e o `PATH` do usuário no `PATH` dos filhos, hoje ninguém sabe se o `postgres.exe` encontra essas DLLs porque elas estão no runtime ou porque esta máquina as tem em `System32`. A hipótese de que os arquivos soltos são justamente essas DLLs é `TBM`, e o inventário está em §18.

### 2.4 O Windows tem opinião sobre como um servidor sobe

Quatro comportamentos do Windows decidem partes deste RFC. Nenhum aparece numa execução de medição feita a partir de um terminal comum.

- **O PostgreSQL se recusa a rodar com privilégio de administrador.** O `pg_ctl` e o `initdb` contornam isso no Windows criando um token restrito, mas o `postgres.exe` chamado diretamente não faz isso. Um instalador que oferece "abrir o SolidVision ao terminar" costuma abrir o app ainda elevado (§8.3).
- **Um programa de console herda o console de quem o chama, ou ganha um novo.** Chamado por um processo sem console, como uma janela Electron ou um `pythonw`, o `postgres.exe` abre uma janela preta na área de trabalho do fotógrafo. Chamado de um terminal, ele divide o console com o terminal, e um Ctrl+C dado ali chega também ao servidor (§8.3).
- **O Hyper-V, o WSL e o Docker Desktop reservam faixas de portas TCP.** Uma porta reservada não está "em uso": o `bind` falha com acesso negado (`WSAEACCES`), e não com endereço em uso (`WSAEADDRINUSE`). Esta máquina tem o Docker Desktop instalado (§8.2).
- **Os executáveis do PostgreSQL recebem os argumentos pela página de código ANSI.** Esse é o comportamento de todo programa C com `main` estreito no Windows. A hipótese é que um caminho com caracteres fora dessa página de código chegue trocado ao `initdb` e ao `pg_ctl` (§10).

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Onde fica | `app/infrastructure/database/embedded/`, sem porta no Domain (§4.1) |
| Binários | Materializados de um **lockfile explícito**, com os 16 pacotes e o `md5` de cada um, num diretório `runtime/postgres` que fica fora do git (§5.1) |
| Localização | Setting `embedded_postgres_runtime_directory`, com default relativo à raiz do código e nunca o `PATH`. O `PATH` dos filhos é montado pelo adaptador, e todas as variáveis `PG*` são removidas (§5.2, §5.3) |
| Diretório do usuário | `%LOCALAPPDATA%\SolidVision\postgres\16\`, com a versão maior no caminho e uma DACL explícita (só o usuário e `SYSTEM`) (§6) |
| Primeira execução | `initdb` num diretório temporário, renomeado no fim. Um lock de transição impede dois `initdb` simultâneos (§7.1) |
| Autenticação | scram, com senha aleatória (`secrets.token_urlsafe(24)`) gravada antes do `initdb` e nunca escrita em log (§7.2) |
| Papel | Um só, `solidvision`, superusuário, como no Docker de desenvolvimento (§7.3) |
| Banco e migrations | `CREATE DATABASE` quando o banco não existe. Alembic na mesma conexão, e só quando o head do banco difere do head do código. Um banco **mais novo** que o código faz o adaptador recusar a subida (§7.4) |
| Configuração do servidor | Um arquivo `solidvision.conf`, incluído no `postgresql.conf` e reescrito pelo adaptador antes de cada subida. Nada vai por `-o` (§8.1) |
| Porta | Só em `127.0.0.1`. Primeiro tenta a última porta que funcionou. Se ela falhar, pede uma livre ao sistema, até `TBM` tentativas. A porta do servidor é lida de volta do `postmaster.pid` (§8.2) |
| Subida e parada | `pg_ctl` com `CREATE_NO_WINDOW`, saída para arquivo. A parada usa `fast` e, se falhar, `immediate`. O adaptador nunca mata o postmaster (§8.3, §8.5) |
| Cluster já de pé | É adotado, não reiniciado, desde que seja deste diretório e deste runtime (§8.4) |
| Encerramento sujo | Uma tabela de estados observados e ações (§9.1). O adaptador **nunca apaga o `postmaster.pid`** (§9.2) e só mata filhos órfãos que são seus (§9.3) |
| Caminhos fora da página de código | Decididos pela medição, com os dois ramos escritos antes dela (§10) |
| Até o RFC-034 | `python -m app.infrastructure.database.embedded run -- <comando>` sobe o cluster e passa as `DATABASE_*` ao comando (§11) |
| Docker no desenvolvimento | **Continua.** Um `pytest` puro continua indo ao Docker, e o embarcado é opcional até o RFC-036 |

## 4. Arquitetura

### 4.1 Onde fica, e por que não há porta no Domain

Nenhum caso de uso sobe um banco. O ciclo de vida do cluster é implantação, como a construção da engine em `engine.py`, que também não tem porta. Uma `DatabaseLifecyclePort` no Domain seria uma interface sem nenhum consumidor nas camadas de cima. O consumidor é a raiz de composição: hoje o CLI deste RFC, depois o launcher do RFC-036.

Pelo mesmo motivo, **nenhuma classe do adaptador lê `settings`**. Elas recebem caminhos no construtor, e só o `__main__` do CLI lê o `Settings`. É a regra que o projeto já segue para os casos de uso, e é ela que deixa os testes de §12 apontarem o adaptador para um diretório temporário. O adaptador também não importa `app.infrastructure.persistence.engine`. Importá-la construiria uma engine apontada para o `.env` antes de o cluster existir.

O adaptador recusa ser construído fora de `win32`, como o `WindowsVolumeIdentityProvider`. A inspeção de processos usa `ctypes` sobre o `kernel32` (`CreateToolhelp32Snapshot`, `QueryFullProcessImageNameW`, `TerminateProcess`), no mesmo estilo do adaptador de volume, sem dependência nova.

| módulo | responsabilidade |
| --- | --- |
| `runtime.py` | `PostgresRuntime`: localiza e confere os binários, lê o manifesto e monta o ambiente dos filhos (§5) |
| `cluster_home.py` | `ClusterHome`: os caminhos do usuário, a DACL, a senha e o lock de transição (§6, §7.1, §7.2) |
| `postmaster_pid.py` | Lê o `postmaster.pid`: PID, diretório, porta e estado (§8.2, §9) |
| `windows_processes.py` | Faz o snapshot de processos, informa o caminho do executável e o processo pai, e encerra um processo (§9.3) |
| `cluster.py` | `EmbeddedPostgres`: a máquina de estados de §9.1, `initdb`, subida, adoção e parada |
| `database_setup.py` | Executa `CREATE DATABASE`, compara heads, roda o Alembic e confere os fatos do cluster (§7.4) |
| `__main__.py` | O CLI: `start`, `stop`, `status` e `run -- <comando>` (§11) |

### 4.2 O contrato

```python
cluster = EmbeddedPostgres(
    runtime=PostgresRuntime(settings.embedded_postgres_runtime_directory),
    home=ClusterHome(settings.embedded_postgres_directory),
)
started = cluster.start(on_phase=report)  # cria se preciso; sobe ou adota
cluster.prepare(started.connection)       # banco e migrations, idempotente
cluster.status()                          # ausente | parado | subindo | pronto | outra versão
cluster.stop()                            # fast; immediate se o fast falhar
```

O `start()` devolve a conexão (`127.0.0.1`, porta, usuário, senha e banco) e três fatos: se **este** chamador subiu o cluster ou o adotou, se o cluster foi criado agora e se a subida passou por recuperação de crash. Com o primeiro fato, o RFC-036 decide quem para o banco no fim (§16). O `on_phase` informa `initdb`, `subida`, `recuperação` e `migrations` à medida que acontecem, porque a primeira execução leva de 25 a 39 s (RFC-033a §13) e uma tela parada durante esse tempo parece travada.

O host é o literal `127.0.0.1`, e não `localhost`. O servidor escuta só em IPv4, e o literal tira da conexão a resolução de nomes.

## 5. O runtime

### 5.1 Um lockfile, não três pins

O RFC-033a fixou `postgresql`, `pgvector` e `libpq` por versão e por build, e registrou no log o ambiente explícito que o solver produziu, com 16 pacotes. Os outros 13 pacotes (OpenSSL, ICU, krb5, libxml2, zlib, o runtime do Visual C++…) só estão fixados no log. Uma nova materialização no mês que vem pode resolver outro OpenSSL.

O ambiente explícito passa a ser a fonte. `scripts/postgres-win-64.lock` é a saída de `micromamba list --explicit --md5`, com as 16 URLs e o `md5` de cada pacote, versionada no git. `scripts/vendor_postgres.py` baixa o micromamba 2.9.0 com o `sha256` do RFC-033a, cria o ambiente **a partir do lockfile**, sem solver, remove os `.pdb` como o RFC-033a fez e grava um manifesto:

```text
runtime/postgres/SOLIDVISION-RUNTIME.json
    postgresql 16.15, pgvector 0.8.6, sha256 do lockfile, data, tamanho podado
```

O diretório `runtime/` fica fora do git (`.gitignore`). São 123 MB que o lockfile reproduz byte a byte, e é desse mesmo diretório que o instalador vai copiar.

A poda continua sendo só a dos `.pdb`. O ADR-008 diz que o RFC-033 *"pode medir o que dá para tirar"*. A medição entra em §18, mas a remoção fica com o instalador. Os 47 MB do ICU são o maior item, e provavelmente não saem: o servidor não usa o ICU, mas o `libxml2` usa, e o `postgres.exe` carrega o `libxml2`. Isso também é `TBM`.

### 5.2 Onde o adaptador procura

O setting `embedded_postgres_runtime_directory` tem como default `<raiz do código>\runtime\postgres`. A raiz é a mesma âncora que o `.env` e os logs já usam (`Path(__file__).parents[4]` em `settings.py`). No desenvolvimento, isso é a raiz do repositório. Instalado, é onde o instalador puser o código, e o runtime vai ao lado. O RFC-034 decide como essa raiz é conhecida num app congelado. Este RFC só garante que o caminho é configurável e absoluto. O prefixo `EMBEDDED_` evita o espaço de nomes `POSTGRES_*`, que a imagem do Docker lê do mesmo `.env`.

Em cada subida, o `PostgresRuntime` confere o manifesto e a presença de `postgres.exe`, `initdb.exe`, `pg_ctl.exe` e `lib\vector.dll`. Ele **não** confere hashes: 123 MB lidos de um HDD a cada subida custariam mais que a subida inteira. A integridade dos arquivos é responsabilidade do instalador. Sem manifesto, o adaptador recusa o diretório. Apontar o adaptador para uma instalação qualquer do PostgreSQL, como a do EnterpriseDB em `Program Files`, falha de forma explícita, em vez de funcionar com outra versão.

### 5.3 "Nunca pelo `PATH`" vale para as DLLs

Os executáveis são chamados pelo caminho absoluto. O ambiente dos filhos é montado, e não herdado:

- **`PATH`** recebe a raiz do runtime, `Library\bin`, `%SystemRoot%\System32` e `%SystemRoot%`, nessa ordem, e nada do `PATH` do usuário. Uma instalação do PostgreSQL 15 no `PATH` do usuário não pode emprestar uma `libssl` ao servidor.
- **Toda variável `PG*` sai**, como o RFC-033a já fazia. `PGDATA`, `PGPORT` e `PGOPTIONS` definidos no ambiente do usuário mudariam o cluster que o `pg_ctl` controla, ou as opções do servidor.

A pergunta de §2.3 tem resposta nesta máquina sem precisar de uma máquina limpa (§15). §18 pede que toda DLL importada pelos quatro binários seja resolvida dentro do runtime ou entre as DLLs do próprio Windows. A verificação lê a tabela de importação dos executáveis com um leitor de PE escrito com a biblioteca padrão.

## 6. O diretório do usuário

```text
%LOCALAPPDATA%\SolidVision\
    thumbnails\                 RFC-030, já existe
    postgres\                   DACL explícita: usuário e SYSTEM
        16\
            data\               PGDATA
            password            uma linha
            adapter.lock        lock de transição (§7.1)
            startup.log         saída do pg_ctl -l, reescrita a cada subida (§8.1)
```

**`LOCALAPPDATA`, pelo motivo dos thumbnails.** O `APPDATA` acompanha o usuário entre máquinas num domínio, e o `LOCALAPPDATA` não. O OneDrive também não sincroniza o `LOCALAPPDATA`, e um cliente de sincronização que tranca ou reescreve arquivos dentro de um cluster ativo corrompe o cluster. O setting `embedded_postgres_directory` existe para teste e diagnóstico. Seu default é absoluto e por usuário, como o de `thumbnail_directory`.

**A versão maior no caminho.** O `postgres\17\` de uma atualização futura cabe ao lado do `16\` sem renomear nada. O `pg_upgrade` precisa dos dois lados ao mesmo tempo.

**A DACL é explícita, e não herdada.** Na criação de `postgres\`, o adaptador remove a herança e concede controle total ao SID do usuário do processo, lido do token, e ao `SYSTEM` (`S-1-5-18`). Ele faz isso pelo `icacls.exe` de `System32`, chamado pelo caminho absoluto. O usuário é identificado **pelo SID, e não pelo nome**: no Windows em português, o grupo `Everyone` se chama `Todos`, e um nome traduzido não pode decidir uma permissão. O `%LOCALAPPDATA%` já costuma herdar só o usuário, o `SYSTEM` e os administradores. A DACL explícita protege o caso em que a herança não é essa, como um perfil redirecionado. Ela também tira os administradores, que ainda podem tomar posse do diretório. O que sai é a leitura casual da senha por outra conta.

## 7. Primeira execução

### 7.1 `initdb` atômico

O `initdb` leva de 24,7 a 38,9 s nesta máquina (RFC-033a §13). É tempo de sobra para o fotógrafo fechar a janela no meio. Um `data\` pela metade não pode ser confundido com um cluster.

1. O adaptador grava `password.tmp`, que é renomeado para `password`.
2. Roda o `initdb` em `16\data.initdb`, com `--auth=scram-sha-256 --pwfile=… --encoding=UTF8 --locale=C -U solidvision`, os mesmos flags do RFC-033a.
3. Com retorno 0, renomeia `data.initdb` para `data`. No mesmo volume, o rename é atômico.

Um `data.initdb` encontrado na subida é sempre resto de uma criação interrompida. O adaptador o apaga e recomeça. Um `password` sem `data` é descartado e gerado de novo, porque a senha e o cluster formam um par.

**O lock de transição.** Dois processos chamando `start()` ao mesmo tempo numa máquina sem cluster rodariam dois `initdb` no mesmo diretório. O adaptador toma um lock exclusivo em `adapter.lock` (`msvcrt.locking`) durante as transições: criação, subida e parada. Ele não segura o lock enquanto o cluster vive. Quem chega depois espera e então adota. O sistema operacional solta o lock quando o processo morre, então um processo morto no meio de um `initdb` não deixa o diretório trancado.

### 7.2 A senha

Mesmo com o servidor só em loopback, existe senha. O loopback é alcançável por **toda conta** da máquina, e não só pela do fotógrafo. Com `trust`, outro usuário do mesmo computador leria o acervo indexado de quem o indexou.

A senha é `secrets.token_urlsafe(24)`, como no RFC-033a. O alfabeto (`A-Z a-z 0-9 _ -`) importa. O `Settings.database_url` interpola a senha no URL sem escapar nada, e uma senha com `@`, `:` ou `/` quebraria a conexão de um jeito difícil de diagnosticar. A senha nunca vai para log, nem do app nem do servidor, e o `status` do CLI não a imprime.

Guardar a senha no Gerenciador de Credenciais ou cifrá-la com DPAPI não protege contra nada que a DACL de §6 deixe passar. As duas formas protegem contra outras contas, e a DACL já faz isso. Contra a própria conta do usuário, nenhuma delas adianta: o `data\` é legível por ela, e a senha precisa chegar em claro à libpq de qualquer jeito (§13).

### 7.3 Um papel só, superusuário

O `initdb -U solidvision` cria o superusuário `solidvision`, e o app conecta como ele. É o que o desenvolvimento já faz: na imagem do Docker, `POSTGRES_USER` é superusuário. Um segundo papel, sem superusuário, para o app seria defesa em profundidade contra quem já tem a senha. Só que quem tem a senha já está dentro da conta do usuário, e dentro dela o `data\` é legível diretamente. A separação custaria uma senha a mais e uma divergência com o dev, e não protegeria nenhuma fronteira nova (§13).

### 7.4 O banco e as migrations

`prepare()` faz três coisas, nesta ordem, todas idempotentes:

1. **O banco.** Se `solidvision` não existe em `pg_database`, o adaptador roda `CREATE DATABASE solidvision TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'`. O encoding e a localidade são explícitos, para não depender do estado de `template1`.
2. **As migrations.** O adaptador lê `alembic_version` e os heads do `ScriptDirectory` do projeto.
   - Head igual: não faz nada. Uma consulta resolve.
   - Banco atrás: roda `alembic upgrade head` **no mesmo processo**, passando a conexão em `config.attributes["connection"]`, que é o padrão do próprio Alembic para compartilhar uma conexão. O `env.py` ganha poucas linhas: se recebeu uma conexão, usa essa; se não, faz o que faz hoje. `alembic upgrade head` na linha de comando continua igual.
   - Banco com uma revisão que o código não conhece: **recusa**, com uma mensagem que diz isso. É o que acontece quando um app mais antigo abre um banco migrado por um mais novo. Rodar o código contra um esquema que ele não conhece é pior que não subir. Tratar downgrades fica fora do roadmap. Detectá-los custa uma comparação.
3. **Os fatos.** Uma consulta confere a versão maior, `encoding = UTF8`, `datcollate = datctype = C` e `vector` instalado na versão do manifesto. Se algum deles divergir, o cluster não é o que o RFC-033a mediu, e o adaptador diz qual campo divergiu. Um pgvector instalado mais antigo que o do runtime gera aviso, e não recusa: o `ALTER EXTENSION vector UPDATE` pertence às atualizações (§14).

A extensão em si quem cria é a migration `999b801e80f4` (§2.3).

**O custo de rodar o Alembic sempre** (`upgrade head` sem nada a fazer: importação, `env.py`, conexão) é `TBM`. **O custo de comparar antes** também é `TBM`. A comparação foi escolhida porque é uma consulta, e a medição diz quanto isso economiza a cada subida.

## 8. Subida, porta e parada

### 8.1 A configuração que o adaptador escreve

O `postgresql.conf` gerado pelo `initdb` ganha uma linha, `include_if_exists = 'solidvision.conf'`, como no RFC-033a. O adaptador reescreve o `solidvision.conf` antes de cada subida:

```ini
listen_addresses = '127.0.0.1'
port = 51336                         # §8.2
log_line_prefix = '%m [%p] '         # milissegundos, como no RFC-033a
logging_collector = on
log_directory = 'log'                # dentro de data\
log_filename = 'postgresql-%a.log'   # um arquivo por dia da semana
log_rotation_age = 1d
log_rotation_size = 0
log_truncate_on_rotation = on        # no máximo sete arquivos
```

**Nada vai por `pg_ctl -o`.** As aspas de uma linha de comando aninhada no Windows são uma fonte conhecida de defeitos. Com tudo no arquivo, quem roda o `pg_ctl` à mão para diagnosticar um problema sobe o mesmo servidor, na mesma porta.

**Dois logs.** O coletor rotaciona o log do servidor. A hipótese, `TBM`, é que ele só começa depois que os sockets já foram abertos. Por isso a causa de uma falha de subida, como um `bind` recusado, iria para o `startup.log`, que é o arquivo de `pg_ctl -l`. Esse é o arquivo que o adaptador lê quando a subida falha. O `--locale=C` do `initdb` já pôs `lc_messages = 'C'` no `postgresql.conf`, então as mensagens saem em inglês em qualquer Windows. É isso que permite ao adaptador reconhecer uma falha de `bind` pelo texto.

O ajuste fino do servidor (`shared_buffers`, `work_mem`, `max_wal_size`) fica nos defaults. Nenhuma medição diz que ele importa, e a inferência em CPU, a ~450 ms por imagem, domina tudo o que o banco faz durante uma indexação (§14).

### 8.2 A porta

**A porta não é 5432, nem nenhuma porta fixa.** A 5432 é a do Docker de desenvolvimento nesta mesma máquina, e é a de qualquer PostgreSQL que o usuário já tenha instalado. Qualquer número fixo pode estar ocupado ou dentro de uma faixa que o Hyper-V reservou, e o sistema operacional é o único que sabe quais estão livres.

A política:

1. O adaptador tenta a porta da última subida que funcionou, que está no `solidvision.conf`.
2. Antes de subir, sonda a porta com um `bind` em `127.0.0.1`. Tanto `WSAEADDRINUSE` quanto `WSAEACCES` contam como "não serve". A segunda é a da faixa reservada (§2.4).
3. Se a porta não serve, faz um `bind` na porta 0, e o sistema devolve uma porta livre fora das faixas excluídas. O adaptador reescreve o `solidvision.conf` com ela.
4. Entre a sonda e a subida, outro processo pode tomar a porta. Se o `startup.log` mostrar que o `bind` falhou, o adaptador volta ao passo 3, até `TBM` tentativas. Uma falha por qualquer outro motivo **não** gera nova tentativa: vira erro, com o fim do `startup.log`.

**A porta de um servidor de pé é lida do `postmaster.pid`.** A linha 4 do `postmaster.pid` é a porta, e o próprio servidor a escreve. A linha 8 é o estado (`starting`, `ready` ou `stopping`). O `solidvision.conf` registra a porta que o adaptador **pediu**. O `postmaster.pid` registra a porta que o servidor **está usando**. Um arquivo de estado próprio, com a porta, seria uma terceira fonte capaz de discordar das duas (§13).

Faixas reservadas podem ser listadas sem privilégio (`netsh interface ipv4 show excludedportrange protocol=tcp`). Quais faixas existem nesta máquina é `TBM`, e §18 usa uma delas, se houver, para testar o passo 2.

### 8.3 Por que `pg_ctl`, e como ele é chamado

O `postgres.exe` chamado diretamente daria ao adaptador o handle do processo e a saída por pipe. Em troca, perderia o token restrito, e o servidor se recusaria a subir a partir de um processo elevado (§2.4). O `pg_ctl` cria esse token e espera o servidor ficar pronto lendo o `postmaster.pid`. No Windows, ele também sabe se o postmaster morreu durante a espera, porque guarda o handle do processo que criou.

O adaptador chama `pg_ctl start -w -t <T> -D … -l startup.log`:

- **Com `CREATE_NO_WINDOW`.** O `pg_ctl` ganha um console sem janela, e o servidor o herda. A hipótese, `TBM`, é que isso resolva os dois problemas de §2.4 de uma vez: nenhuma janela aparece quando o chamador não tem console, e um Ctrl+C no terminal de quem chamou não chega a um console que o servidor não divide com ele.
- **Com a saída para arquivo, e nunca para pipe.** O servidor herdaria o pipe, e quem esperasse o fim do `pg_ctl` esperaria o fim do servidor. O RFC-033a §14 registrou essa precaução, e o RFC-036 vai encontrar a mesma herança.
- **`T` é o tempo de uma subida normal com folga** (`TBM`). Se o prazo vencer com o `postmaster.pid` em `starting`, o servidor está em recuperação de crash, e não travado. O adaptador continua esperando enquanto o processo estiver vivo, até um teto derivado do tempo de recuperação medido (`TBM`). Durante a espera, informa `recuperação` pelo `on_phase`.

O `pg_ctl -w` verifica o estado a cada 100 ms. Contra um `listening → ready` de 0,216 s (RFC-033a), essa granularidade pode pesar. O custo dela entra na decomposição da subida a quente (§18). Esperar pelo próprio adaptador, com `pg_ctl -W`, economizaria em média 50 ms, mas perderia a detecção de um postmaster morto antes de escrever o `postmaster.pid`.

### 8.4 Adoção

Um `start()` que encontra o cluster de pé **não o reinicia**. Ele adota o cluster se três coisas forem verdade:

- a linha 2 do `postmaster.pid` aponta para este `data\`;
- o PID da linha 1 está vivo, e o executável dele está **dentro deste runtime**;
- uma conexão com a senha do `password` é aceita.

Se o executável estiver em outro runtime, por exemplo um cluster subido pelo app instalado enquanto o desenvolvedor roda o código do repositório, o adaptador recusa e diz qual runtime está segurando o cluster (§15). Se a senha for recusada, o par senha-cluster se perdeu, e o adaptador recusa em vez de tentar outra coisa.

É a adoção que torna inofensivo o órfão de um launcher morto: a próxima subida o encontra e o usa. Se ele deve ou não sobreviver ao launcher é uma política do RFC-036 (§16).

### 8.5 Parada

`pg_ctl stop -m fast -w -t 60`. Se falhar, `-m immediate`, como no RFC-033a. O modo `fast` desfaz as transações abertas e faz um checkpoint. O `immediate` não faz checkpoint, e a próxima subida faz a recuperação pelo WAL, o que é seguro e só mais lento. O `smart` esperaria os clientes se desconectarem, e quem para o banco é quem já parou os clientes.

**O adaptador nunca mata o postmaster.** Se o `immediate` também falhar, ele informa a falha. Matar o postmaster é sempre seguro para os dados, mas deixa filhos para trás (§9.3), e decidir quando isso vale a pena é do launcher.

## 9. Encerramento sujo

### 9.1 O que o adaptador observa, e o que faz

| observado | o que significa | ação |
| --- | --- | --- |
| `data.initdb` existe | Um `initdb` foi interrompido | Apagar `data.initdb` e recriar (§7.1) |
| `data` não existe | Primeira execução | `initdb` atômico |
| `data\PG_VERSION` diferente da versão maior do manifesto | É um cluster de outra versão | **Recusar.** Nunca rodar `initdb` por cima nem apagar |
| Sem `postmaster.pid` | Parada limpa, ou o cluster nunca subiu | Subir |
| `postmaster.pid` com PID vivo, deste runtime, em `ready` | O cluster já está de pé | Adotar (§8.4) |
| O mesmo, em `starting` | Está subindo ou recuperando | Esperar o `ready` (§8.3) |
| O mesmo, em `stopping` | Está parando | Esperar o PID sumir, depois subir |
| `postmaster.pid` com PID morto | Encerramento sujo: Gerenciador de Tarefas, queda do launcher, logoff | `pg_ctl start`. O próprio PostgreSQL reconhece o lock órfão e recupera pelo WAL |
| A subida falha com memória compartilhada ainda em uso | Filhos do postmaster morto ainda vivos | Esperar `TBM` s, encerrar só os órfãos que são deste cluster (§9.3) e tentar **uma** vez mais |
| A subida falha por `bind` | Porta tomada | §8.2, passo 4 |
| Qualquer outra falha | — | Erro com o fim do `startup.log`, sem nova tentativa |

### 9.2 O adaptador nunca apaga o `postmaster.pid`

O `postmaster.pid` é o lock do PostgreSQL sobre o diretório de dados. Apagá-lo com um servidor vivo permite que um segundo postmaster suba sobre o mesmo `data\`, e dois postmasters no mesmo diretório corrompem o cluster. O próprio servidor já sabe distinguir um lock órfão: no Windows, ele verifica se o PID anotado ainda responde pelo canal de sinais do PostgreSQL, e um PID reaproveitado por outro programa não responde. O adaptador decide **o que fazer** a partir do `postmaster.pid`, mas quem decide se o lock vale é o servidor.

### 9.3 Órfãos: só mata o que é seu

Quando o postmaster morre à força, os processos filhos (backends, checkpointer, walwriter) percebem e saem sozinhos, normalmente logo. Enquanto não saem, seguram a memória compartilhada, e uma nova subida falha. Quanto tempo eles sobrevivem é `TBM`.

Se for preciso encerrá-los, o adaptador encerra **somente** processos que atendem às duas condições:

- o pai, segundo o snapshot de processos, é o PID morto anotado no `postmaster.pid` **deste** `data\`;
- o executável está dentro **deste** runtime.

As duas condições juntas protegem o PostgreSQL que o usuário instalou por conta própria e o cluster de outra conta do Windows na mesma máquina. Esse segundo cenário é real: duas contas, cada uma com seu SolidVision, cada uma com seu `LOCALAPPDATA` e sua porta. Um `postgres.exe` de isca, rodando de uma cópia do runtime em outro diretório, precisa sobreviver à limpeza (§18).

### 9.4 O que é testado, e o que não pode ser

O critério do roadmap é que matar o `postgres.exe` à força e reiniciar recupere sem intervenção manual, e que isso seja *"testado, não presumido"*. §18 cobre isso com carga de escrita em andamento. Um cliente grava linhas numa tabela de teste e anota o último commit que o servidor **confirmou**. Depois da recuperação, toda transação confirmada precisa estar lá. Os cenários são:

- o postmaster morto com `taskkill /F`;
- a árvore inteira morta com `taskkill /F /T`;
- só um backend morto (o postmaster reinicia os outros e o cluster continua de pé);
- `pg_ctl stop -m immediate`;
- o `initdb` morto no meio;
- uma subida morta durante a recuperação de crash;
- o logoff do Windows com o cluster de pé (manual).

**Uma queda de energia não é testável aqui.** Ela depende do que o disco faz com o cache de escrita, e esta máquina não tem como simular isso sem uma VM. O que este RFC garante é não piorar a situação: `fsync`, `full_page_writes` e `synchronous_commit` ficam nos defaults, que estão ligados, e nenhum deles é desligado em troca de velocidade.

## 10. Caminhos com espaço e acento

O roadmap pede o teste com `C:\Users\João Silva\...`. O RFC-033a mostrou que a página de código ANSI desta máquina é a 1252 (`Portuguese_Brazil.1252`), e `João` cabe nela. O caso que pode quebrar é outro: um caractere **fora** da página de código, como um nome `Łukasz` numa máquina em 1252, ou um nome em ideogramas.

**A hipótese, `TBM`.** Os executáveis do PostgreSQL recebem `argv` pela página de código ANSI, e um caractere fora dela chega trocado. Ele pode virar `?`, ou um parecido pelo mapeamento *best-fit* (`Ł` → `L`), o que aponta para outro caminho. O mesmo vale para os caminhos que o servidor abre por APIs não Unicode.

| caso | caminho de teste |
| --- | --- |
| Diretório do usuário com espaço e acento dentro da 1252 | `…\João Silva\SolidVision\postgres` |
| Diretório do usuário com caracteres fora da 1252 | `…\Łukasz 東京\SolidVision\postgres` |
| Runtime com espaço | O runtime copiado para `…\Solid Vision Runtime\postgres` |

**Os dois ramos, escritos antes da medição.** Se o segundo caso funcionar, nada muda. Se falhar, o adaptador confere antes de chamar o `initdb` se o caminho cabe na página de código ANSI (`GetACP()`). Se não couber, usa o nome curto 8.3 do diretório (`GetShortPathNameW`), que é ASCII. Se o volume não gerar nomes 8.3, o adaptador recusa com uma mensagem que nomeia o problema e o setting `EMBEDDED_POSTGRES_DIRECTORY` como saída. O mesmo teste vale para o runtime, e o resultado dele é uma restrição que o instalador herda: o diretório de instalação precisa caber na página de código (§16).

## 11. Como o resto do sistema encontra o banco, até o RFC-034

Nada em `engine.py`, `session.py`, nos repositórios, na API ou no `job_runner` muda. A ponte é a do RFC-033a §4.2, e quem monta as variáveis é o CLI:

```text
python -m app.infrastructure.database.embedded start
python -m app.infrastructure.database.embedded status
python -m app.infrastructure.database.embedded run -- python -m pytest
python -m app.infrastructure.database.embedded stop
```

`run -- <comando>` faz `start()` e `prepare()`, e então executa o comando com `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_USER`, `DATABASE_PASSWORD` e `DATABASE_NAME` no ambiente. O código de saída do comando vira o código de saída do `run`. **O `run` deixa o cluster como o encontrou**: se foi ele que subiu o cluster, ele para o cluster no fim, e se adotou, não para. Os nomes das variáveis vêm de uma constante só, e um teste confere que cada uma corresponde a um campo do `Settings`. Isso fecha, no código, a armadilha que o RFC-033a §4.2 fechou com uma verificação de porta.

O desenvolvimento continua no Docker. Um `pytest` puro vai ao `.env`, como hoje. O embarcado no dia a dia é opcional até o RFC-036 oferecer o mesmo comando para os dois mundos. O RFC-034 substitui as variáveis: o `Settings` instalado vai ler a conexão do adaptador, e não do ambiente.

## 12. Testes

**Unitários, na suíte padrão.** A máquina de estados de §9.1 é testada linha a linha com dublês para o runtime, o sistema de arquivos do cluster, o `postmaster.pid` e a tabela de processos. Não há `initdb` nem processo real, então os testes seguem offline e rápidos, como a suíte padrão exige. Entram aqui:

- a escolha de porta, com `WSAEADDRINUSE` e `WSAEACCES` simulados;
- a regra de órfãos de §9.3, com uma isca de outro runtime e outra de outro pai;
- a leitura do `postmaster.pid`;
- a montagem do ambiente dos filhos (nenhum `PG*`, nenhum diretório do `PATH` do usuário);
- a comparação de heads, incluindo a revisão desconhecida.

**Integração, com o marcador `embedded`.** Esses testes usam o runtime de verdade, e por isso ficam fora do default, como o `slow`. O `addopts` passa a `-m 'not slow and not embedded'`, e eles rodam com `pytest -m embedded`. Para não pagar um `initdb` por teste, uma fixture de sessão cria **um** cluster parado num diretório temporário, e cada teste que precisa de um cluster novo recebe uma **cópia** desse `data\`. Copiar um cluster parado produz um cluster válido. Com o runtime ausente, os testes são pulados com uma mensagem que diz como materializar o runtime, e não falham.

**A suíte inteira contra o cluster do adaptador.** É o critério do roadmap, medido como o RFC-033a mediu: `run -- python -m pytest` com o Docker desligado, a porta conferida pelo `settings` do projeto num subprocesso e o `xact_commit` subindo (§18).

## 13. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Uma porta no Domain para o ciclo de vida do banco | Nenhum caso de uso a consumiria. O consumidor é a raiz de composição (§4.1) |
| O banco como serviço do Windows (`pg_ctl register`) | Exige administrador para instalar, roda numa conta de serviço que não enxerga o `LOCALAPPDATA` do usuário, fica ligado mesmo com o app fechado, e é um cluster por máquina, não por usuário. Se o RFC-036 escolher serviço para o launcher, ele herda esta restrição |
| Chamar o `postgres.exe` direto | Sem o token restrito do `pg_ctl`, o servidor não sobe a partir de um processo elevado (§8.3) |
| Socket Unix em vez de TCP | O PostgreSQL aceita sockets Unix no Windows desde a versão 13, e com eles não haveria porta para escolher. Mas o caminho do socket tem limite de 108 bytes e já começa em `C:\Users\<nome>\AppData\Local\…`, a questão da página de código dobra (§10), e não foi verificado se o build do conda-forge habilita o recurso, nem como o psycopg aceita um caminho como host no Windows. Fica como medição futura |
| Porta fixa, 5432 ou outra | A 5432 é a do Docker de desenvolvimento nesta máquina e a de qualquer PostgreSQL já instalado. Qualquer número fixo pode estar numa faixa reservada (§8.2) |
| Uma porta nova a cada subida, como no script do RFC-033a | Funciona, mas troca a porta sem motivo e atrapalha o diagnóstico. Reaproveitar a última que funcionou não custa nada |
| Um arquivo de estado próprio com a porta | Seria uma segunda verdade, capaz de discordar do `postmaster.pid`, que o próprio servidor escreve (§8.2) |
| Apagar um `postmaster.pid` "órfão" | Se o servidor estiver vivo, dois postmasters sobem no mesmo diretório (§9.2) |
| Rodar o `initdb` no instalador | O instalador roda elevado, às vezes com a conta de outro usuário, e o cluster cairia no `LOCALAPPDATA` errado. Numa instalação por máquina, cada usuário precisa do próprio cluster |
| Levar no instalador um `data\` pronto e copiá-lo | Talvez mais rápido que o `initdb` (o custo da cópia é `TBM`). Em troca, todas as instalações sairiam com a mesma senha e o mesmo identificador de sistema, e a senha teria de ser trocada em modo single-user antes de abrir a porta. É uma decisão do instalador, e a medição fica registrada para ele |
| `initdb --no-sync` | Mais rápido, mas uma queda de energia logo depois da primeira execução deixaria um cluster com cara de completo. O rename não força os arquivos para o disco. Quanto do `initdb` é `fsync` entra em §18 como diagnóstico, não como opção |
| Senha no Gerenciador de Credenciais ou cifrada com DPAPI | Não protege contra nada que a DACL deixe passar, e a senha precisa chegar em claro à libpq (§7.2). O `keyring` seria uma dependência nova |
| `trust` em loopback | Qualquer conta da máquina conectaria (§7.2) |
| Dois papéis: superusuário e papel do app | Não cria fronteira nova contra quem já tem a senha, e diverge do desenvolvimento (§7.3) |
| Um passo `CREATE EXTENSION` separado | Duplica a migration `999b801e80f4` (§2.3) |
| O Alembic num subprocesso, como no RFC-033a | Um Python a mais em toda subida, e dependente das variáveis de ambiente de §2.2 |
| Rodar `alembic upgrade head` sempre | Idempotente, mas importa o Alembic e roda o `env.py` em toda subida. Comparar os heads custa uma consulta (§7.4; os dois custos são `TBM`) |
| Listar processos pelo PowerShell | Segundos por chamada (§2.1) |
| `psutil` | Uma dependência nova para três chamadas ao `kernel32` |
| Binários no git, ou no Git LFS | 123 MB no histórico, quando um lockfile de 16 linhas reproduz o mesmo diretório byte a byte |
| Fixar só `postgresql`, `pgvector` e `libpq`, como no RFC-033a | Os outros 13 pacotes, entre eles o OpenSSL e o runtime do Visual C++, flutuariam (§5.1) |
| Recompor o runtime numa árvore própria (`bin\`, `lib\`, `share\`) | O PostgreSQL acha `share\` e `lib\` relativos ao executável, e o layout do conda já funciona, como o RFC-033a provou. Mudar o layout seria um risco sem ganho |

## 14. Não-objetivos

- **O launcher** (RFC-036): instância única, política de reinício, se o banco sobrevive ao launcher, job object, encerramento no logoff.
- **O `Settings` sem checkout** (RFC-034): separar desenvolvimento e instalação, credenciais lidas do adaptador, `log_directory` absoluto e a raiz da instalação num app congelado.
- **O instalador:** payload, poda além dos `.pdb`, assinatura, exclusões do Defender, escolha do diretório de instalação e cópia de um `data\` pronto.
- **Atualizações:** `pg_upgrade` entre versões maiores, `ALTER EXTENSION vector UPDATE` e migrations entre versões do app. Um banco mais novo que o código é detectado e recusado (§7.4), não tratado.
- **Backup e restauração** do cluster.
- **Ajuste fino** do servidor (§8.1).
- **Tirar o Docker do desenvolvimento** (§11).
- **Sockets Unix** (§13).
- **Linux e macOS.** O adaptador recusa ser construído fora de `win32` (§4.1).
- **Um banco de teste dedicado.** A ressalva de `empty_db_session`, em `tests/conftest.py`, continua valendo.

## 15. Riscos

| risco | situação |
| --- | --- |
| Caminhos fora da página de código | Hipótese, com os dois ramos escritos (§10) |
| O runtime do Visual C++ numa máquina limpa | Não dá para testar aqui. Esta máquina roda Windows 10 Home, que não tem o Windows Sandbox. O fechamento de DLLs de §5.3 substitui o teste até o instalador ter uma máquina limpa |
| Antivírus e SmartScreen | As medições rodam com o Defender ligado, o padrão. No desenvolvimento, o runtime fica dentro do repositório. Onde ele fica numa instalação é decisão do instalador |
| Desenvolvimento e app instalado na mesma máquina dividem o `postgres\` padrão | Uma migration do código em desenvolvimento faria o app instalado recusar o banco (§7.4). O adaptador recusa adotar um cluster de outro runtime (§8.4). A separação de fato é do RFC-034. Até lá, o desenvolvedor usa `EMBEDDED_POSTGRES_DIRECTORY` |
| O `pg_ctl` no Windows põe o postmaster num job object próprio | A confirmar, junto com como isso interage com um job object do launcher. Isso é do RFC-036, e este RFC só registra a pergunta |
| O logoff mata o cluster | A próxima subida recupera pelo WAL, o que é correto e mais lento. É medido à mão (§18). Parar o banco no logoff é do RFC-036 |
| A falha de `bind` é reconhecida pelo texto | O texto é estável porque `lc_messages = 'C'`. Se o reconhecimento falhar, a falha cai em "qualquer outra", e o erro sai com o log, sem nova tentativa |
| O `initdb` variou de 24,7 a 38,9 s sem causa medida | §18 mede quanto dele é `fsync` |
| Pacotes do lockfile removidos do conda-forge | O `vendor_postgres.py` falha de forma explícita. O instalador leva o runtime pronto, então só o desenvolvimento é afetado |
| A suíte quebrar contra o cluster do adaptador | Seria um achado, não ruído: o cluster tem os mesmos flags do RFC-033a, que passou a mesma suíte. A diferença estaria no adaptador |

## 16. O que passa adiante

**Para o RFC-034:** o adaptador devolve uma conexão completa (§4.2). O `Settings` instalado deve lê-la dali, e não de `DATABASE_*`. A raiz da instalação, de que depende o default de `embedded_postgres_runtime_directory`, também é dele. E é ele que separa o `postgres\` do desenvolvimento do `postgres\` do app instalado.

**Para o RFC-036:**

- O `start()` diz se quem chamou subiu o cluster ou o adotou, e com isso o launcher decide quem para o banco.
- Um cluster sem dono é adotado na próxima subida, e não perdido. Matá-lo junto com o launcher, por job object, é uma escolha, não uma necessidade.
- O `status()` serve para a supervisão. O `pg_isready` não é necessário.
- O `on_phase` existe para a tela da primeira execução.
- O banco não pode ser serviço do Windows se o cluster é por usuário (§13).

**Para o instalador:**

- O diretório de instalação precisa caber na página de código ANSI, se §10 confirmar a hipótese.
- O runtime do Visual C++ precisa ir junto, se §18 mostrar que o `System32` não basta.
- O `initdb` roda na primeira execução de cada usuário, nunca na instalação.
- A medição da cópia de um `data\` pronto fica registrada em §18.

## 17. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-033-postgres-embarcado.md` | Este documento |
| `backend/app/infrastructure/database/embedded/` | O adaptador e o CLI (§4.1) |
| `scripts/postgres-win-64.lock` | O ambiente explícito, com 16 pacotes e `md5` (§5.1) |
| `scripts/vendor_postgres.py` | Materializa o runtime a partir do lockfile, poda os `.pdb` e grava o manifesto (§5.1) |
| `backend/tests/infrastructure/database/embedded/` | Unitários na suíte padrão e integração sob `embedded` (§12) |
| `experiments/rfc-033-embedded-postgres/measure_adapter.py` (+ `.log`, fora do git) | §18 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/alembic/env.py` | Usa a conexão de `config.attributes["connection"]` quando a recebe (§7.4) |
| `backend/app/infrastructure/config/settings.py` | `embedded_postgres_runtime_directory` e `embedded_postgres_directory`, com defaults absolutos (§5.2, §6) |
| `pyproject.toml` | Marcador `embedded` e `addopts` (§12) |
| `.gitignore` | `runtime/` |
| `backend/README.md` | Como materializar o runtime e usar `run --` |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-standalone.md` | O status deste RFC |
| `docs/adr/adr-008-postgresql-embarcado.md` | `Implementado por:` aponta para este documento, na implementação |

Não mudam: `engine.py`, `session.py`, os repositórios, a API, o `job_runner`, o `docker-compose.yml` e nenhum arquivo do Domain ou da Application.

## 18. Validação

Na máquina do RFC-033a, com o Docker desligado, salvo onde a linha diz outra coisa.

**A regra de não regressão, declarada antes da medição:** nenhuma parcela que o RFC-033a mediu pode piorar além da variação que o próprio RFC-033a registrou. Os limites são o `initdb` de 24,7 a 38,9 s, o `listening → ready` de 0,216 a 0,235 s e o `alembic upgrade head`, com a cadeia inteira, de 2,4 a 2,5 s.

| verificação | como | resultado |
| --- | --- | --- |
| Primeira execução | `postgres\` vazio, com o runtime já materializado; `start()` e `prepare()` | `TBM` s no total, com `initdb`, subida, `CREATE DATABASE` e migrations medidos separadamente |
| Quanto do `initdb` é `fsync` | `initdb` com e sem `--no-sync`, como diagnóstico (§13) | `TBM` |
| Copiar um `data\` pronto, para o instalador | Copiar o `data\` de um cluster parado (38 MB) | `TBM` s |
| Subida a quente | Cluster parado; do `start()` até a conexão aceita | `TBM` s, decomposto em conferência do runtime, `pg_ctl -w`, leitura do `postmaster.pid`, conexão e comparação de heads |
| Adoção | Cluster de pé; `start()` | `TBM` s |
| Migrations numa subida sem nada a migrar | `upgrade head` sem nada a fazer, contra a comparação de heads | `TBM` contra `TBM` |
| Parada | `stop()`, modo `fast` | `TBM` s; nenhum `postgres.exe` na lista de processos ao final |
| Memória ociosa | Soma do working set dos `postgres.exe` com o cluster parado em `ready` | `TBM` MB |
| A suíte contra o cluster do adaptador | `run -- python -m pytest`, Docker desligado; porta conferida pelo `settings` num subprocesso e `xact_commit` subindo (RFC-033a §4.2) | `TBM` passed, `TBM` deselected |
| `pytest -m embedded` | Os testes de integração de §12 | `TBM` |
| Os fatos do cluster | Os campos do passo 8 do RFC-033a, lidos do cluster do adaptador | `TBM`: iguais aos do RFC-033a |
| Postmaster morto à força | `taskkill /F` no PID do postmaster, com escrita em andamento; depois `start()` | `TBM`: sobe sem intervenção, nenhuma transação confirmada perdida, recuperação em `TBM` s |
| Árvore morta | `taskkill /F /T`, com escrita em andamento | `TBM`, com os mesmos critérios |
| Um backend morto | `taskkill /F` em um backend | `TBM`: o cluster continua de pé e a próxima conexão funciona |
| `immediate` | `pg_ctl stop -m immediate`; depois `start()` | `TBM` |
| Órfãos | Tempo de vida dos filhos depois da morte do postmaster; uma isca de outro runtime sobrevive à limpeza | `TBM` s; `TBM` |
| `initdb` interrompido | Morto aos `TBM` s; depois `start()` | `TBM`: `data.initdb` apagado e recriado |
| Subida morta durante a recuperação | Morta em `starting`; depois `start()` | `TBM` |
| Porta ocupada | Um socket escutando na última porta; `start()` | `TBM`: sobe em outra porta, que é lida do `postmaster.pid` |
| Faixa reservada | `netsh … show excludedportrange`; se houver uma faixa, a última porta é forçada para dentro dela | Faixas nesta máquina: `TBM`. Resultado: `TBM` |
| Dois `start()` simultâneos | Dois processos, máquina sem cluster | `TBM`: um cria, o outro adota; um único postmaster |
| Processo elevado | `start()` de um PowerShell como administrador | `TBM` |
| Sem console | `start()` chamado por `pythonw` | `TBM`: nenhuma janela |
| Ctrl+C | Ctrl+C no terminal de `run -- <comando longo>` | `TBM`: o servidor não cai pelo Ctrl+C; o `run` para o que ele mesmo subiu |
| Caminhos | Os três casos de §10 | `TBM`, `TBM`, `TBM`, e qual ramo de §10 foi adotado |
| DLLs | Tabela de importação dos quatro binários, resolvida contra o runtime e o Windows; inventário da raiz do ambiente | `TBM`; `TBM` |
| Ambiente do usuário | `PGPORT`, `PGDATA` e `PGOPTIONS` definidos, e uma pasta com outra `libpq.dll` no `PATH` | `TBM`: nada muda |
| DACL | Lida por SID | `TBM`: só o usuário e `S-1-5-18` |
| A senha não vaza | Busca pela senha nos logs do app, no log do servidor e na saída do CLI | `TBM`: nenhuma ocorrência |
| Firewall | Primeira subida numa sessão em que o Defender Firewall está ligado | `TBM`: nenhum diálogo |
| Banco mais novo que o código | `alembic_version` com uma revisão desconhecida | `TBM`: recusa com a mensagem, sem escrever no banco |
| Outra versão maior | `PG_VERSION` forjado como `15` | `TBM`: recusa, sem `initdb` por cima |
| Logoff | Logoff do Windows com o cluster de pé; nova sessão; `start()` (manual) | `TBM` |
| Estilo e tipos | `black --check`, `ruff check`, `mypy` | `TBM`: zero erros no código novo |
| Docker continua funcionando | `pytest` puro, com o Docker ligado | `TBM`: a mesma contagem de antes |
