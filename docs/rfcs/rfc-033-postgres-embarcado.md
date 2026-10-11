# RFC-033 — PostgreSQL Embarcado

**Status:** Implementado. Duas verificações de §18 ficaram sem fazer, e estão ditas em §19.3: o logoff e a suíte com o Docker desligado ao pé da letra. O processo elevado foi verificado à mão, depois da execução final: o `start()` funciona, e o token restrito de §8.3 se confirmou, com um controle (§18)
**Etapa:** 1 do [roadmap sem Docker](ROADMAP-standalone.md#rfc-033--postgresql-embarcado-)
**Depende de:** RFC-033a (linha de base e script de medição), [ADR-008](../adr/adr-008-postgresql-embarcado.md) (a decisão de embarcar), RFC-004 (variáveis de ambiente acima do `.env`), RFC-007 (Alembic), RFC-018 (a migration que cria a extensão), RFC-029 §6 (os processos rodam no host), RFC-030 §7.1 (`%LOCALAPPDATA%\SolidVision`)
**Bloqueia:** RFC-034 (as credenciais do banco passam a vir daqui), RFC-036 (o launcher sobe o banco por este adaptador)
**Migration:** não. Nenhuma revisão nova. O `alembic/env.py` passa a aceitar uma conexão recebida de quem o chama (§7.4)
**Medição:** `experiments/rfc-033-embedded-postgres/measure_adapter.py` — medido em 2026-10-11, noutra máquina que não a do RFC-033a (§19.2). `measure_adapter.log` é a execução que este documento cita. Os dois ensaios do mesmo dia, feitos antes de o script ganhar as últimas verificações, ficaram como `measure_adapter.rehearsal.log` e `measure_adapter.rehearsal-2.log`, e servem para ver quanto a máquina varia. A linha de base desta máquina, o script do RFC-033a rodado aqui duas vezes, está em `measure_embedded_postgres.2026-10-11.log` e `measure_embedded_postgres.2026-10-11-b.log`. A verificação do processo elevado, feita à mão, está em `measure_adapter.elevated.log` (a execução das 18h26, que sobrescreveu as anteriores), e as mesmas leituras sem elevação estão em `measure_adapter.not-elevated.log`. Todos ficam ao lado dos scripts e fora do git, como o log de todo experimento

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os números medidos que este documento cita vêm do RFC-033a (2026-10-08, HDD SATA `ST1000DM010-2EP102`, Docker desligado), e cada um diz de onde veio. Onde o texto afirma como o Windows ou o PostgreSQL se comportam sem ter medido, a frase diz que é hipótese, e §18 diz como ela vai ser testada.
>
> **Isso foi feito.** Na implementação, em 2026-10-11, cada `TBM` virou um número ou um resultado, escrito à vista da frase original, em notas como esta. Onde a medição contrariou o texto, o texto ficou e a correção está em §19. Três coisas que este documento presumia caíram, e as três mudaram código: onde o servidor escreve a recusa de um `bind` (§8.1), o que o `pg_ctl start -w` garante (§8.3), e o que `ready` no `postmaster.pid` quer dizer (§8.4). **A máquina não é a do RFC-033a** (§19.2), e duas verificações de §18 não foram feitas, e estão ditas como tal (§19.3).

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

**"Nunca pelo `PATH`" precisa valer também para as DLLs.** O passo 3 do log do RFC-033a mede 5,0 MB de arquivos soltos na raiz do ambiente (*"(top-level files) 5.0"*). O ambiente explícito traz `vc14_runtime`, `vcomp14` e `ucrt`, que formam o runtime do Visual C++, e uma instalação limpa do Windows pode não tê-lo. Como o script pôs a raiz do ambiente e o `PATH` do usuário no `PATH` dos filhos, hoje ninguém sabe se o `postgres.exe` encontra essas DLLs porque elas estão no runtime ou porque esta máquina as tem em `System32`. A hipótese de que os arquivos soltos são justamente essas DLLs era `TBM`, e o inventário está em §18.

> **Medido: a hipótese se confirmou, e a pergunta tem resposta.** Os 5,0 MB soltos na raiz são, por pacote: `ucrt`, 46 arquivos e 2,4 MB; `vc14_runtime`, 11 arquivos e 2,3 MB; `vcomp14`, 1 arquivo e 0,2 MB; e `libzlib`, 1 arquivo (`zlib.dll`) e 0,1 MB. Os mesmos arquivos existem também em `Library\bin`, ao lado dos executáveis.
>
> E o `postgres.exe` encontra o runtime do Visual C++ **porque ele está no runtime**, e não porque esta máquina o tem. Esta máquina tem, de fato, um `vcruntime140.dll` e um `msvcp140.dll` no `System32`, e nenhum dos dois é do Windows: não estão sob a proteção de recursos do sistema, ou seja, foram postos lá por um redistribuível. Um servidor em execução carregou `VCRUNTIME140.dll` de `runtime\postgres\Library\bin`, porque o diretório do executável vem antes do `System32` na ordem de busca. A exceção é a `ucrtbase.dll`, que veio do `System32`, onde ela **é** do Windows (está sob a proteção de recursos). Os binários não a importam pelo nome: importam doze conjuntos de API (`api-ms-win-crt-*`), que o carregador do Windows resolve. As cópias que o pacote `ucrt` traz não foram carregadas.

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
| Porta | Só em `127.0.0.1`. Primeiro tenta a última porta que funcionou. Se ela falhar, pede uma livre ao sistema, até 3 tentativas (era `TBM`). A porta do servidor é lida de volta do `postmaster.pid` (§8.2) |
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

> **Acrescentado na implementação (§19).** Dois módulos a mais. `errors.py` guarda as exceções que os demais módulos levantam (`EmbeddedPostgresError` e as que derivam dela), para que nenhum precise importar outro só por causa delas. `code_page.py` é o segundo ramo de §10, que a medição pôs em vigor.

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

> **Corrigido na implementação (§19).** O `status()` tem sete respostas, e não cinco. Um servidor que está parando não é nenhuma das cinco, e um cluster seguro por outro runtime (§8.4) também não: os dois ganharam nome próprio (`stopping` e `other runtime`). O `prepare()` recebe o `on_phase` também, porque é ele que roda as migrations. E a conexão que o `start()` devolve tem a senha fora do `repr`.

## 5. O runtime

### 5.1 Um lockfile, não três pins

O RFC-033a fixou `postgresql`, `pgvector` e `libpq` por versão e por build, e registrou no log o ambiente explícito que o solver produziu, com 16 pacotes. Os outros 13 pacotes (OpenSSL, ICU, krb5, libxml2, zlib, o runtime do Visual C++…) só estão fixados no log. Uma nova materialização no mês que vem pode resolver outro OpenSSL.

O ambiente explícito passa a ser a fonte. `scripts/postgres-win-64.lock` é a saída de `micromamba list --explicit --md5`, com as 16 URLs e o `md5` de cada pacote, versionada no git. `scripts/vendor_postgres.py` baixa o micromamba 2.9.0 com o `sha256` do RFC-033a, cria o ambiente **a partir do lockfile**, sem solver, remove os `.pdb` como o RFC-033a fez e grava um manifesto:

```text
runtime/postgres/SOLIDVISION-RUNTIME.json
    postgresql 16.15, pgvector 0.8.6, sha256 do lockfile, data, tamanho podado
```

O diretório `runtime/` fica fora do git (`.gitignore`). São 123 MB que o lockfile reproduz byte a byte, e é desse mesmo diretório que o instalador vai copiar.

> **Feito, com três acréscimos (§19).** O lockfile foi gerado de novo, e não copiado do log do RFC-033a: aquele log fica fora do git e não estava na máquina desta implementação. O `vendor_postgres.py --lock` resolve os três pins do RFC-033a e regrava o arquivo; sem a opção, ele só materializa. O resultado tem os mesmos 16 pacotes. O `md5` de cada pacote é conferido pelo próprio script contra o arquivo baixado, e não confiado ao micromamba. E o `sha256` do lockfile que vai para o manifesto é calculado com o fim de linha normalizado, porque o git entrega o arquivo com CRLF ou LF conforme o `core.autocrlf` de cada checkout.

A poda continua sendo só a dos `.pdb`. O ADR-008 diz que o RFC-033 *"pode medir o que dá para tirar"*. A medição entra em §18, mas a remoção fica com o instalador. Os 47 MB do ICU são o maior item, e provavelmente não saem: o servidor não usa o ICU, mas o `libxml2` usa, e o `postgres.exe` carrega o `libxml2`. Isso também era `TBM`.

> **Medido: a maior parte não sai.** As doze DLLs do ICU somam 47,0 MB. Um `postgres.exe` em execução tem duas delas entre os seus módulos, `icudt78.dll` (31,6 MB) e `icuuc78.dll` (2,6 MB), e as duas aparecem no fecho de importações do executável, trazidas pelo `libxml2.dll`. São 34,2 MB que o servidor carrega a cada processo, sem ter sido compilado com ICU. Os 12,8 MB restantes (`icuin78`, `icuio78`, `icutu78`, `icutest78` e as cópias sem número de versão) não foram carregados pelo postmaster nem por um backend que usou o `vector`. Não ser carregado numa medição não prova que nunca será, e tirar ou não fica com o instalador, como este parágrafo já dizia.

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
            pg_ctl.log          o que o próprio pg_ctl imprime, reescrito a cada chamada
```

> **Acrescentado na implementação (§19).** O `pg_ctl.log` é o quinto arquivo. O `-l` leva para o `startup.log` o que o *servidor* escreve. O que o `pg_ctl` escreve (*"another server might be running"*, *"is not a database cluster directory"*) precisava de um destino que não fosse um pipe (§8.3), e os dois não podem dividir o mesmo arquivo.

**`LOCALAPPDATA`, pelo motivo dos thumbnails.** O `APPDATA` acompanha o usuário entre máquinas num domínio, e o `LOCALAPPDATA` não. O OneDrive também não sincroniza o `LOCALAPPDATA`, e um cliente de sincronização que tranca ou reescreve arquivos dentro de um cluster ativo corrompe o cluster. O setting `embedded_postgres_directory` existe para teste e diagnóstico. Seu default é absoluto e por usuário, como o de `thumbnail_directory`.

**A versão maior no caminho.** O `postgres\17\` de uma atualização futura cabe ao lado do `16\` sem renomear nada. O `pg_upgrade` precisa dos dois lados ao mesmo tempo.

**A DACL é explícita, e não herdada.** Na criação de `postgres\`, o adaptador remove a herança e concede controle total ao SID do usuário do processo, lido do token, e ao `SYSTEM` (`S-1-5-18`). Ele faz isso pelo `icacls.exe` de `System32`, chamado pelo caminho absoluto. O usuário é identificado **pelo SID, e não pelo nome**: no Windows em português, o grupo `Everyone` se chama `Todos`, e um nome traduzido não pode decidir uma permissão. O `%LOCALAPPDATA%` já costuma herdar só o usuário, o `SYSTEM` e os administradores. A DACL explícita protege o caso em que a herança não é essa, como um perfil redirecionado. Ela também tira os administradores, que ainda podem tomar posse do diretório. O que sai é a leitura casual da senha por outra conta.

> **Corrigido na implementação (§19).** *"Na criação de `postgres\`"* deixaria uma janela: um processo morto entre o `mkdir` e o `icacls` deixaria o diretório para sempre com a DACL herdada, porque na vez seguinte ele já existiria. A DACL é aplicada no caminho da **primeira execução**, antes de a senha ser gravada, quer o diretório já exista ou não. O `icacls` é idempotente, e isso acontece uma vez por cluster, ao lado de um `initdb`.

## 7. Primeira execução

### 7.1 `initdb` atômico

O `initdb` leva de 24,7 a 38,9 s nesta máquina (RFC-033a §13). É tempo de sobra para o fotógrafo fechar a janela no meio. Um `data\` pela metade não pode ser confundido com um cluster.

1. O adaptador grava `password.tmp`, que é renomeado para `password`.
2. Roda o `initdb` em `16\data.initdb`, com `--auth=scram-sha-256 --pwfile=… --encoding=UTF8 --locale=C -U solidvision`, os mesmos flags do RFC-033a.
3. Com retorno 0, renomeia `data.initdb` para `data`. No mesmo volume, o rename é atômico.

Um `data.initdb` encontrado na subida é sempre resto de uma criação interrompida. O adaptador o apaga e recomeça. Um `password` sem `data` é descartado e gerado de novo, porque a senha e o cluster formam um par.

**O lock de transição.** Dois processos chamando `start()` ao mesmo tempo numa máquina sem cluster rodariam dois `initdb` no mesmo diretório. O adaptador toma um lock exclusivo em `adapter.lock` (`msvcrt.locking`) durante as transições: criação, subida e parada. Ele não segura o lock enquanto o cluster vive. Quem chega depois espera e então adota. O sistema operacional solta o lock quando o processo morre, então um processo morto no meio de um `initdb` não deixa o diretório trancado.

> **Corrigido na implementação (§19).** *"Sempre resto de uma criação interrompida"* presume que o `initdb` morre com quem o chamou, e isso só vale quando a árvore inteira é morta. Morto sozinho (o *Finalizar tarefa* do Gerenciador de Tarefas), o processo que chamou solta o lock e o `initdb.exe` continua escrevendo em `data.initdb`. Por isso, antes de apagar o diretório, o adaptador espera sair qualquer `initdb.exe` **deste runtime** que ainda esteja vivo. Ele espera, e não mata. §18 exercita os dois casos. O rename de `data.initdb` para `data` também é repetido por alguns segundos se o Windows o recusar: um antivírus abrindo os arquivos que o `initdb` acabou de gravar basta para isso.

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

> **Acrescentado na implementação (§19).** Três coisas que "idempotente" exigiu e o texto não dizia.
>
> - **Dois chamadores ao mesmo tempo.** O lock de §7.1 cobre criação, subida e parada, e não o `prepare()`. Dois processos que adotam o mesmo cluster recém-criado veriam o banco atrás e migrariam juntos. O `CREATE DATABASE` que perde a corrida recebe *"already exists"* (`42P04`) e segue. As migrations rodam sob `pg_advisory_xact_lock`, tomado **só** quando o head está atrás, e o head é lido de novo depois dele: quem chega em segundo espera e encontra o banco em dia. O caminho comum, com o head igual, continua sem lock.
> - **As variáveis `PG*` de quem chama.** §5.3 tira as `PG*` do ambiente dos filhos, mas as conexões do próprio adaptador saem do processo de quem o chamou, e a libpq lê `PGOPTIONS`, `PGSSLMODE` e `PGGSSENCMODE` de lá. Um `PGOPTIONS` de só leitura faria o `CREATE DATABASE` falhar. As conexões do adaptador passam os três parâmetros explicitamente (`options=''`, `sslmode=disable`, `gssencmode=disable`), porque a libpq só recorre ao ambiente para um parâmetro que não recebeu.
> - **"Uma consulta resolve" são uma consulta e uma leitura.** Saber o head do código exige o `ScriptDirectory`, que importa o Alembic e lê os arquivos das migrations. É essa parte, e não a consulta, que domina o custo medido abaixo.

**O custo de rodar o Alembic sempre** (`upgrade head` sem nada a fazer: importação, `env.py`, conexão) era `TBM`. **O custo de comparar antes** também era `TBM`. **Medido: 1,04 s contra 0,49 s** (§18; nos ensaios, 0,85 contra 0,51 s e 0,57 contra 0,28 s). A comparação foi escolhida porque é uma consulta, e a medição diz quanto isso economiza a cada subida.

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

**Dois logs.** O coletor rotaciona o log do servidor. A hipótese, que era `TBM`, é que ele só começa depois que os sockets já foram abertos. Por isso a causa de uma falha de subida, como um `bind` recusado, iria para o `startup.log`, que é o arquivo de `pg_ctl -l`. Esse é o arquivo que o adaptador lê quando a subida falha. O `--locale=C` do `initdb` já pôs `lc_messages = 'C'` no `postgresql.conf`, então as mensagens saem em inglês em qualquer Windows. É isso que permite ao adaptador reconhecer uma falha de `bind` pelo texto.

> **Corrigido na implementação: a hipótese era falsa (§19).** O PostgreSQL 16 sobe o coletor **antes** de abrir os sockets. A linha `listening on IPv4 address` está no arquivo do coletor, em `data\log\`, e o `startup.log` de uma subida normal tem duas linhas, que só anunciam o redirecionamento. A recusa de um `bind` vai, portanto, para o log do coletor. Forçado a subir numa porta de faixa reservada, o servidor escreveu lá:
>
> ```text
> LOG:  could not bind IPv4 address "127.0.0.1": Permission denied
> WARNING:  could not create listen socket for "127.0.0.1"
> FATAL:  could not create any TCP/IP sockets
> ```
>
> e o `startup.log` não tinha a recusa. Um adaptador que lesse só o `startup.log`, como este parágrafo descrevia, classificaria toda falha de `bind` como "qualquer outra" e nunca tentaria outra porta. O adaptador lê os dois: o `startup.log`, que continua sendo onde aparece o que vem antes do coletor (o lock do diretório, a configuração, a memória compartilhada), e o que o coletor escreveu **nesta** subida. Os arquivos do coletor são um por dia da semana e acumulam todas as subidas do dia, então o adaptador anota o tamanho de cada um antes de chamar o `pg_ctl` e lê só o que veio depois. Sem isso, a recusa de `bind` de uma subida da manhã seria atribuída à falha da tarde.
>
> O texto das mensagens é estável por um motivo a mais do que o `lc_messages`: este build não tem nenhum catálogo de tradução (nenhum arquivo `.mo` no runtime).

O ajuste fino do servidor (`shared_buffers`, `work_mem`, `max_wal_size`) fica nos defaults. Nenhuma medição diz que ele importa, e a inferência em CPU, a ~450 ms por imagem, domina tudo o que o banco faz durante uma indexação (§14).

### 8.2 A porta

**A porta não é 5432, nem nenhuma porta fixa.** A 5432 é a do Docker de desenvolvimento nesta mesma máquina, e é a de qualquer PostgreSQL que o usuário já tenha instalado. Qualquer número fixo pode estar ocupado ou dentro de uma faixa que o Hyper-V reservou, e o sistema operacional é o único que sabe quais estão livres.

A política:

1. O adaptador tenta a porta da última subida que funcionou, que está no `solidvision.conf`.
2. Antes de subir, sonda a porta com um `bind` em `127.0.0.1`. Tanto `WSAEADDRINUSE` quanto `WSAEACCES` contam como "não serve". A segunda é a da faixa reservada (§2.4).
3. Se a porta não serve, faz um `bind` na porta 0, e o sistema devolve uma porta livre fora das faixas excluídas. O adaptador reescreve o `solidvision.conf` com ela.
4. Entre a sonda e a subida, outro processo pode tomar a porta. Se o `startup.log` mostrar que o `bind` falhou, o adaptador volta ao passo 3, até 3 tentativas (era `TBM`; e o lugar onde a falha aparece não é o `startup.log`, §8.1). Uma falha por qualquer outro motivo **não** gera nova tentativa: vira erro, com o fim do `startup.log`.

**A porta de um servidor de pé é lida do `postmaster.pid`.** A linha 4 do `postmaster.pid` é a porta, e o próprio servidor a escreve. A linha 8 é o estado (`starting`, `ready` ou `stopping`). O `solidvision.conf` registra a porta que o adaptador **pediu**. O `postmaster.pid` registra a porta que o servidor **está usando**. Um arquivo de estado próprio, com a porta, seria uma terceira fonte capaz de discordar das duas (§13).

Faixas reservadas podem ser listadas sem privilégio (`netsh interface ipv4 show excludedportrange protocol=tcp`). Quais faixas existem nesta máquina era `TBM`, e §18 usa uma delas, se houver, para testar o passo 2.

> **Medido (§18).** Os quatro passos, cada um contra o sistema de verdade.
>
> - **Passo 2, as duas recusas.** Uma porta em que outro socket escuta responde `WSAEADDRINUSE` (10048). Uma porta de faixa reservada responde `WSAEACCES` (10013), como §2.4 dizia, e a sonda trata as duas como "não serve". Uma sonda que só conhecesse a primeira deixaria a segunda escapar como erro.
> - **As faixas existem, e se mexem.** Esta máquina, com o Docker Desktop instalado, tem 16 faixas reservadas, doze delas de cem portas, quase todas dentro do intervalo de onde o sistema tira as portas livres (§18 as lista). Numa leitura feita à mão por volta do meio-dia, antes das medições, as três últimas eram 51052–51151, 51152–51251 e 51502–51601. Nos logs, das 16h em diante, são 53988–54087, 57742–57841 e 58424–58523. A primeira leitura não ficou em log, e por isso vale como observação, não como medição. Ela aponta para o argumento de §8.2: a porta que funcionou de manhã pode estar reservada à tarde, e só o sistema sabe.
> - **Passo 3.** O `bind` na porta 0 nunca devolveu uma porta de faixa reservada (§18 tem a contagem).
> - **Passo 4, e o `TBM` das tentativas: 3.** Não é um número medido, é uma escolha. A corrida exige que outro processo tome exatamente aquela porta entre a sonda e a subida. Ela foi forçada, trocando a sonda por uma que sempre responde "livre": com a porta ocupada, e depois com a porta numa faixa reservada, o servidor recusou, o adaptador leu a recusa (no log do coletor, §8.1) e subiu na tentativa seguinte, nas duas vezes. Três tentativas dão duas chances a mais do que as medições precisaram.
> - **A sonda pede a porta com exclusividade** (`SO_EXCLUSIVEADDRUSE`), para responder "não" também a uma porta que outro socket segura de um jeito que deixaria um segundo `bind` dividi-la. Uma medição mostrou por que isso importa pouco para a escolha e muito para o entendimento: com o Docker escutando em `0.0.0.0:5432`, um `bind` em `127.0.0.1:5432` **é aceito**. O Windows deixa o endereço mais específico passar. É mais um motivo para nunca usar a 5432.

### 8.3 Por que `pg_ctl`, e como ele é chamado

O `postgres.exe` chamado diretamente daria ao adaptador o handle do processo e a saída por pipe. Em troca, perderia o token restrito, e o servidor se recusaria a subir a partir de um processo elevado (§2.4). O `pg_ctl` cria esse token e espera o servidor ficar pronto lendo o `postmaster.pid`. No Windows, ele também sabe se o postmaster morreu durante a espera, porque guarda o handle do processo que criou.

O adaptador chama `pg_ctl start -w -t <T> -D … -l startup.log`:

- **Com `CREATE_NO_WINDOW`.** O `pg_ctl` ganha um console sem janela, e o servidor o herda. A hipótese, que era `TBM`, é que isso resolva os dois problemas de §2.4 de uma vez: nenhuma janela aparece quando o chamador não tem console, e um Ctrl+C no terminal de quem chamou não chega a um console que o servidor não divide com ele. **Medido: confirmada nos dois casos, cada um com um controle que falha sem a flag (§18).** Sem ela, o mesmo `pg_ctl` chamado pelo mesmo `pythonw` abriu uma janela do Windows Terminal, e o servidor que dividia o console de quem o chamou caiu com o Ctrl+C.
- **Com a saída para arquivo, e nunca para pipe.** O servidor herdaria o pipe, e quem esperasse o fim do `pg_ctl` esperaria o fim do servidor. O RFC-033a §14 registrou essa precaução, e o RFC-036 vai encontrar a mesma herança.
- **`T` é o tempo de uma subida normal com folga** (era `TBM`: **30 s**). Se o prazo vencer com o `postmaster.pid` em `starting`, o servidor está em recuperação de crash, e não travado. O adaptador continua esperando enquanto o processo estiver vivo, até um teto derivado do tempo de recuperação medido (era `TBM`: **600 s**). Durante a espera, informa `recuperação` pelo `on_phase`. **Medido (§18):** o `pg_ctl start -w` levou de 0,6 a 1,9 s numa subida limpa (33 subidas), e até 9,6 s com um crash para recuperar. Os 30 s não venceram em nenhuma subida medida, e o caminho de depois do prazo é coberto pelos testes de unidade, com um servidor simulado. **Os 600 s não saíram de uma medição.** A recuperação mais longa medida levou 13,4 s, num SSD, e o teto é uma escolha com folga de umas 45 vezes sobre ela. O que um HDD precisa não foi medido (§19.3).

O `pg_ctl -w` verifica o estado a cada 100 ms. Contra um `listening → ready` de 0,216 s (RFC-033a), essa granularidade pode pesar. O custo dela entra na decomposição da subida a quente (§18). Esperar pelo próprio adaptador, com `pg_ctl -W`, economizaria em média 50 ms, mas perderia a detecção de um postmaster morto antes de escrever o `postmaster.pid`.

> **Corrigido na implementação: o código de saída do `pg_ctl` é um indício, e não a resposta (§19).** *"O `pg_ctl` espera o servidor ficar pronto lendo o `postmaster.pid`"* é verdade, e é também o defeito. No Windows o que o `pg_ctl` cria é um `cmd.exe`, pai do postmaster, então ele **não tem como conferir** se o PID do arquivo é o do servidor que acabou de subir. O que lhe resta é a idade do arquivo: ele aceita um `postmaster.pid` recente. O arquivo **órfão** de um postmaster que acabou de ser morto passa nesse teste, e ainda diz `ready`. O `pg_ctl start -w` devolveu sucesso com o servidor novo em recuperação, e o login seguinte recebeu *"the database system is starting up"*.
>
> **Medido (§18).** O pai do postmaster é um `cmd.exe`, que o `pg_ctl` inicia como `cmd.exe /D /C ""…\postgres.exe" -D "…\data" < "nul" >> "…\startup.log" 2>&1"`. Um servidor foi morto, e o `pg_ctl start -w` foi chamado em seguida, quatro vezes, variando há quanto tempo o servidor morto tinha subido, que é a hora anotada na linha 3 do `postmaster.pid`. Com 2,1 s e 2,5 s, o `pg_ctl` devolveu 0 em 0,20 s e 0,26 s: o `postmaster.pid` ainda era o do servidor morto, dizendo `ready`, e um login naquele instante foi recusado com *"the database system is starting up"*. Com 3,5 s e 6,0 s, ele esperou 4,9 s e 4,7 s e devolveu 0 com o arquivo do servidor novo e o login aceito. O ensaio deu o mesmo corte: enganado com 2,1 s e 2,4 s, certo com 4,3 s e 6,2 s. **E acontece sem ser provocado:** em duas das seis mortes do postmaster de §18, e em duas das doze mortes de árvore da tentativa de reprodução de §9.3, o `pg_ctl` voltou em menos de 0,4 s com o servidor ainda a segundos de ficar pronto.
>
> O adaptador trata o retorno do `pg_ctl` como indício. A resposta é um `postmaster.pid` cujo PID está **vivo**, é um `postgres.exe` **deste runtime** e aponta para **este** `data\` — as mesmas três condições da adoção (§8.4) — e o que esse arquivo diz. Se o `pg_ctl` disse sim e o arquivo ainda é o órfão, o adaptador espera o servidor novo reescrevê-lo. Um `FATAL` no `startup.log`, ou uma recusa de `bind` no log do coletor, encerra essa espera antes do prazo: aquele servidor não vem.

> **Medido, de um processo elevado e de um comum (§18): confirmado, com um controle que é recusado sem o `pg_ctl`.** Os tokens foram lidos nos dois casos, pelo mesmo leitor.
>
> | token de | sem elevação | de um processo elevado |
> | --- | --- | --- |
> | quem chama (`python.exe`) | Administradores só para negar; 5 privilégios | Administradores **valendo**; 24 privilégios |
> | o `cmd.exe` que o `pg_ctl` cria | só para negar; 1 | **só para negar**; 1 |
> | o postmaster | só para negar; 1 | **só para negar**; 1 |
> | um filho do postmaster | só para negar; 1 | **só para negar**; 1 |
>
> - **O `pg_ctl` cria o token restrito.** De um processo elevado, quem chama tem o grupo Administradores valendo e 24 privilégios. O primeiro processo que o `pg_ctl` cria já tem o grupo só para negar acesso, e um privilégio. O postmaster e o filho dele têm o mesmo. O binário do `pg_ctl` traz a mensagem do caminho que faz isso (*"restricted token: error code"*).
> - **O controle foi recusado.** O `postgres.exe` chamado direto pelo mesmo processo elevado, sem o `pg_ctl`, saiu com código 1 e a mensagem que começa em *"Execution of PostgreSQL by a user with administrative permissions is not"*. Sem elevação, o mesmo comando só reclama do diretório, que não existe de propósito. É a frase do texto, *"o servidor se recusaria a subir a partir de um processo elevado"*, medida.
> - **`TokenElevation` não mostra nada disso.** No processo elevado ele deu `True` para o postmaster, para o `cmd.exe` e para o filho, com o tipo de elevação `full`, mesmo com o grupo Administradores só para negar. Quem precisar saber se um servidor roda com direitos de administrador (RFC-036) tem de ler o grupo, e não esse bit. A primeira versão da verificação lia só o bit, e a primeira execução que passou, a das 18h18, não respondeu a pergunta por isso.
> - **O `initdb` aponta na mesma direção.** Chamado pelo processo elevado, ele não entrou num diretório cuja ACL só nomeava o sistema, os administradores e o proprietário, que era o grupo Administradores, e entrou assim que o SID do usuário foi acrescentado. Um processo com o grupo Administradores valendo teria entrado nos dois casos. É uma inferência sobre o `initdb`, tirada de duas medições, e não uma leitura do token dele. O binário traz a mensagem do caminho que faria isso (*"could not re-execute with restricted token"*).
>
> **A consequência prática é sobre diretórios, e interessa ao instalador e ao launcher (§16).** Uma pasta criada por um processo elevado tem o grupo Administradores como dono. Se a ACL dela não nomear o usuário, o `initdb` chamado por esse mesmo processo não entra nela: foi o que aconteceu cinco vezes com o diretório de trabalho do script. A DACL que o adaptador aplica em `postgres\` (§6) nomeia o SID do usuário, e foi debaixo dela que as duas execuções elevadas funcionaram.

### 8.4 Adoção

Um `start()` que encontra o cluster de pé **não o reinicia**. Ele adota o cluster se três coisas forem verdade:

- a linha 2 do `postmaster.pid` aponta para este `data\`;
- o PID da linha 1 está vivo, e o executável dele está **dentro deste runtime**;
- uma conexão com a senha do `password` é aceita.

Se o executável estiver em outro runtime, por exemplo um cluster subido pelo app instalado enquanto o desenvolvedor roda o código do repositório, o adaptador recusa e diz qual runtime está segurando o cluster (§15). Se a senha for recusada, o par senha-cluster se perdeu, e o adaptador recusa em vez de tentar outra coisa.

É a adoção que torna inofensivo o órfão de um launcher morto: a próxima subida o encontra e o usa. Se ele deve ou não sobreviver ao launcher é uma política do RFC-036 (§16).

> **Corrigido na implementação: `ready` no arquivo não é `ready` no servidor (§19).** A terceira condição não é só uma conferência de senha. Quando **um** processo do servidor morre, o postmaster encerra os outros e reinicializa. Durante esses segundos toda conexão é recusada com *"the database system is in recovery mode"*, e o `postmaster.pid` **não é reescrito**: ele continua dizendo `ready`. Um `start()` que chegasse nessa janela adotaria o cluster e devolveria um erro, ou pior, uma conexão que ainda não funciona. A recusa *"ainda não"* (`cannot_connect_now`) passou a ser a única que o adaptador espera passar, até o mesmo teto da recuperação, informando `recuperação` pelo `on_phase`. Senha recusada continua sendo recusa, sem segunda tentativa.

### 8.5 Parada

`pg_ctl stop -m fast -w -t 60`. Se falhar, `-m immediate`, como no RFC-033a. O modo `fast` desfaz as transações abertas e faz um checkpoint. O `immediate` não faz checkpoint, e a próxima subida faz a recuperação pelo WAL, o que é seguro e só mais lento. O `smart` esperaria os clientes se desconectarem, e quem para o banco é quem já parou os clientes.

**O adaptador nunca mata o postmaster.** Se o `immediate` também falhar, ele informa a falha. Matar o postmaster é sempre seguro para os dados, mas deixa filhos para trás (§9.3), e decidir quando isso vale a pena é do launcher.

> **Corrigido na implementação: "parou" é quando os processos saíram (§19).** O `pg_ctl stop -w` espera o `postmaster.pid` sumir, e o postmaster o apaga a caminho da saída. No instante em que o `pg_ctl` volta, alguns dos processos do servidor ainda existem (§18 tem a contagem e o tempo). Para quem chamou, é a diferença entre "parei" e "não há mais `postgres.exe` deste cluster", que é o critério de §18 e o que o RFC-036 vai exigir do launcher. Para o próprio adaptador, é o `startup.log` ainda trancado na subida seguinte (§9.1). O `stop()` espera esses processos saírem. Ele não encerra ninguém: o lock já foi solto, e o que sobra está saindo.
>
> Essa espera confere cada processo **pela identidade**, e não pelo número: precisa ser um programa deste runtime, e, se for um filho, ter o postmaster como pai. A primeira versão perguntava só "os PIDs que eu vi continuam vivos?", e numa de 115 paradas esperou os cinco segundos inteiros por alguém que não saiu. **A causa não foi identificada.** A explicação mais provável é que um processo guarda o PID do pai uma vez, e o Windows nunca o atualiza: nesta máquina, 18 de 328 processos apontavam como pai um PID que já não era de ninguém. Quando o postmaster recebe um desses números, esses processos aparecem numa listagem como filhos dele. A conferência pela identidade tira esse caso. Se a causa foi outra, o aviso de "processos ainda saindo" continua no código para mostrá-la.

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
| A subida falha com memória compartilhada ainda em uso | Filhos do postmaster morto ainda vivos | Esperar 5 s (era `TBM`; §9.3), encerrar só os órfãos que são deste cluster (§9.3) e tentar **uma** vez mais |
| A subida falha por `bind` | Porta tomada | §8.2, passo 4 |
| Qualquer outra falha | — | Erro com o fim do `startup.log`, sem nova tentativa |

> **Corrigido na implementação (§19).** A tabela ganhou uma linha, e uma das suas linhas mudou de lugar na ordem dos acontecimentos.
>
> | observado | o que significa | ação |
> | --- | --- | --- |
> | O `startup.log` não pode ser reescrito | Filhos do postmaster morto ainda vivos: todo processo do servidor mantém esse arquivo aberto | A mesma da memória compartilhada: esperar, encerrar só os órfãos que são deste cluster (§9.3), tentar de novo |
> | `ready` no `postmaster.pid`, e o login recusado com *"in recovery mode"* | Um processo do servidor morreu, e o postmaster está reinicializando | Esperar o login ser aceito (§8.4) |
>
> A linha da memória compartilhada descreve o que **não chegou a acontecer** em nenhuma das medições. O `pg_ctl` sobe o servidor por um `cmd.exe`, que abre o `startup.log` para ele, e a partir daí o arquivo não pode ser aberto para escrita por mais ninguém enquanto houver **um** processo daquele servidor vivo. O adaptador reescreve o `startup.log` antes de cada subida (§8.1), e é nesse passo, antes de qualquer servidor novo chegar à memória compartilhada, que os órfãos aparecem. A regra de §9.3 é aplicada ali, sem mudar: espera, só os deste cluster, uma vez. A linha da memória compartilhada continua no código, para o caso de o arquivo estar livre e a memória não.

### 9.2 O adaptador nunca apaga o `postmaster.pid`

O `postmaster.pid` é o lock do PostgreSQL sobre o diretório de dados. Apagá-lo com um servidor vivo permite que um segundo postmaster suba sobre o mesmo `data\`, e dois postmasters no mesmo diretório corrompem o cluster. O próprio servidor já sabe distinguir um lock órfão: no Windows, ele verifica se o PID anotado ainda responde pelo canal de sinais do PostgreSQL, e um PID reaproveitado por outro programa não responde. O adaptador decide **o que fazer** a partir do `postmaster.pid`, mas quem decide se o lock vale é o servidor.

### 9.3 Órfãos: só mata o que é seu

Quando o postmaster morre à força, os processos filhos (backends, checkpointer, walwriter) percebem e saem sozinhos, normalmente logo. Enquanto não saem, seguram a memória compartilhada, e uma nova subida falha. Quanto tempo eles sobrevivem era `TBM`.

> **Medido (§18): cerca de 0,05 s, e a falha que eles causam não é a da memória compartilhada (§9.1).** Com o cluster ocioso, o último filho some entre 0,05 e 0,17 s depois do postmaster, em quinze mortes. Sob carga de escrita, de 3 a 6 dos sete ainda existem no instante em que o `taskkill` volta, e todos saem antes que o `start()` seguinte precise deles. A espera ficou em **5 s**, que é trinta vezes o pior caso medido.
>
> **Uma vez o prazo venceu sem que ninguém o forçasse.** Em uma de 27 mortes de árvore (`taskkill /F /T`), dois filhos passaram dos 5 s, e o adaptador os encerrou pela regra abaixo. O `start()` levou 13,7 s e nenhuma transação se perdeu. **A causa não foi identificada.** Depois disso o script passou a anotar quem sobrevive e se o processo nasceu durante a própria morte, e o caso não se repetiu nas 18 mortes seguintes. Para exercitar a regra de propósito, §18 suspende um filho antes de matar o postmaster.

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

> **Feito (§18).** Os seis cenários que a máquina consegue exercitar sozinha foram exercitados, e nenhuma transação confirmada se perdeu em nenhum deles. O cliente grava uma linha por transação e anota a última que o servidor confirmou; depois da recuperação, todas até ela precisam estar lá, e estavam. O `initdb` foi morto dos dois jeitos que importam, a árvore inteira e só quem o chamou (§7.1). O sétimo cenário, o logoff, não foi feito (§19.3).
>
> O critério do roadmap, *"testado, não presumido"*, pegou o que a presunção não pegaria. Duas das três correções que mudaram código vieram desta lista: a de §8.3, ao matar o postmaster e subir logo em seguida, e a de §8.4, ao matar um backend. A terceira, a de §8.1, veio de forçar uma porta recusada (§8.2).

## 10. Caminhos com espaço e acento

O roadmap pede o teste com `C:\Users\João Silva\...`. O RFC-033a mostrou que a página de código ANSI desta máquina é a 1252 (`Portuguese_Brazil.1252`), e `João` cabe nela. O caso que pode quebrar é outro: um caractere **fora** da página de código, como um nome `Łukasz` numa máquina em 1252, ou um nome em ideogramas.

**A hipótese, que era `TBM`.** Os executáveis do PostgreSQL recebem `argv` pela página de código ANSI, e um caractere fora dela chega trocado. Ele pode virar `?`, ou um parecido pelo mapeamento *best-fit* (`Ł` → `L`), o que aponta para outro caminho. O mesmo vale para os caminhos que o servidor abre por APIs não Unicode.

| caso | caminho de teste |
| --- | --- |
| Diretório do usuário com espaço e acento dentro da 1252 | `…\João Silva\SolidVision\postgres` |
| Diretório do usuário com caracteres fora da 1252 | `…\Łukasz 東京\SolidVision\postgres` |
| Runtime com espaço | O runtime copiado para `…\Solid Vision Runtime\postgres` |

**Os dois ramos, escritos antes da medição.** Se o segundo caso funcionar, nada muda. Se falhar, o adaptador confere antes de chamar o `initdb` se o caminho cabe na página de código ANSI (`GetACP()`). Se não couber, usa o nome curto 8.3 do diretório (`GetShortPathNameW`), que é ASCII. Se o volume não gerar nomes 8.3, o adaptador recusa com uma mensagem que nomeia o problema e o setting `EMBEDDED_POSTGRES_DIRECTORY` como saída. O mesmo teste vale para o runtime, e o resultado dele é uma restrição que o instalador herda: o diretório de instalação precisa caber na página de código (§16).

> **Medido: a hipótese se confirmou, e o segundo ramo está em vigor (§19).** Página de código 1252, como no RFC-033a.
>
> | caso | sem o ramo | com o ramo |
> | --- | --- | --- |
> | `…\João Silva\SolidVision\postgres` | Funciona. Todo caractere de `João Silva` está na 1252 | Igual: o caminho não é tocado |
> | `…\Łukasz 東京\SolidVision\postgres` | **Falha.** O `initdb` recebeu `…\Lukasz ??\…` e não achou o arquivo de senha: *"could not open file …\Lukasz ??\SolidVision\postgres\16\password"* | Funciona, pelo nome curto `…\UKASZ~1\SOLIDV~1\postgres` |
> | Runtime em `…\Solid Vision Runtime\postgres` | Funciona | Igual |
> | Runtime em `…\Łukasz 東京 runtime\postgres` *(acrescentado)* | **Falha.** O `initdb` não achou a si mesmo: *"invalid binary …\Lukasz ?? runtime\postgres\Library\bin\initdb.exe"* | Funciona, pelo nome curto `…\UKASZR~1\postgres` |
>
> As duas formas de troca que a hipótese previa apareceram no mesmo caminho: o `Ł` virou `L` pelo *best-fit*, o que aponta para **outro** diretório, e os ideogramas viraram `?`. Por isso o teste de "cabe na página de código" usa a conversão estrita, que recusa a letra parecida em vez de aceitá-la.
>
> O ramo foi aplicado aos dois lados, e não só ao diretório do usuário. O nome curto do runtime funcionou igualmente, então a restrição que o instalador herda ficou **menor** do que a frase acima previa: o diretório de instalação só precisa caber na página de código num volume **sem** nomes 8.3 (§16). Num volume assim o adaptador recusa, com uma mensagem que nomeia a página de código e o setting a mudar (`EMBEDDED_POSTGRES_DIRECTORY` ou `EMBEDDED_POSTGRES_RUNTIME_DIRECTORY`). Esse ramo da recusa é coberto por um teste unitário; esta máquina não tem volume sem nomes 8.3 para medi-lo, e criar um exige privilégio de administrador.
>
> O `status()` continua mostrando o caminho como o usuário o conhece. O nome curto só existe na linha de comando dos executáveis, no `PATH` dos filhos e na linha 2 do `postmaster.pid`.
>
> **Um efeito colateral no caso que funciona.** Com `João Silva`, o servidor guarda o próprio `data_directory` na página de código, dentro de um banco `UTF8`. Um cliente que peça esse valor como texto (`SHOW data_directory`, ou qualquer leitura de `pg_settings`) recebe bytes que não são UTF-8 válido, e o psycopg falha ao decodificá-los. Nada no projeto lê esses valores hoje. Fica registrado porque o usuário com acento no nome é o caso comum, não o raro.

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

> **Feito como descrito, com três detalhes que o uso decidiu (§19).**
>
> - **De onde se roda.** O pacote é `app.…`, e `app` está em `backend\`. A partir da raiz do repositório, que é de onde o `pytest` precisa rodar para achar o `pyproject.toml`, os quatro comandos pedem `backend` no caminho de módulos (`$env:PYTHONPATH = "backend"`). O `backend/README.md` tem a sequência inteira, conferida como está escrita.
> - **O que o CLI imprime vai para o `stderr`**, para que o `stdout` de `run -- <comando>` continue sendo só o do comando. O `status` é a exceção, porque a resposta dele é a saída.
> - **O `start` também prepara o banco.** Um `start` que deixasse o banco sem as migrations obrigaria todo mundo a lembrar de um segundo passo. `start` e `run` fazem `start()` e `prepare()`; se o `prepare()` recusar o banco (§7.4), o `run` para o que subiu e não roda o comando.

## 12. Testes

**Unitários, na suíte padrão.** A máquina de estados de §9.1 é testada linha a linha com dublês para o runtime, o sistema de arquivos do cluster, o `postmaster.pid` e a tabela de processos. Não há `initdb` nem processo real, então os testes seguem offline e rápidos, como a suíte padrão exige. Entram aqui:

- a escolha de porta, com `WSAEADDRINUSE` e `WSAEACCES` simulados;
- a regra de órfãos de §9.3, com uma isca de outro runtime e outra de outro pai;
- a leitura do `postmaster.pid`;
- a montagem do ambiente dos filhos (nenhum `PG*`, nenhum diretório do `PATH` do usuário);
- a comparação de heads, incluindo a revisão desconhecida.

**Integração, com o marcador `embedded`.** Esses testes usam o runtime de verdade, e por isso ficam fora do default, como o `slow`. O `addopts` passa a `-m 'not slow and not embedded'`, e eles rodam com `pytest -m embedded`. Para não pagar um `initdb` por teste, uma fixture de sessão cria **um** cluster parado num diretório temporário, e cada teste que precisa de um cluster novo recebe uma **cópia** desse `data\`. Copiar um cluster parado produz um cluster válido. Com o runtime ausente, os testes são pulados com uma mensagem que diz como materializar o runtime, e não falham.

**A suíte inteira contra o cluster do adaptador.** É o critério do roadmap, medido como o RFC-033a mediu: `run -- python -m pytest` com o Docker desligado, a porta conferida pelo `settings` do projeto num subprocesso e o `xact_commit` subindo (§18).

> **Feito (§19).** São 169 testes na suíte padrão e 21 sob `embedded`. Dos 169, 69 são a máquina de estados de §9.1.
>
> Uma diferença do que este parágrafo descreve: **o sistema de arquivos do cluster não é um dublê.** Os dublês são três, e cobrem tudo o que toca a máquina: os comandos (`initdb` e `pg_ctl`), a tabela de processos e a sonda de porta. Os arquivos do cluster são arquivos de verdade, num diretório temporário, porque eles **são** o estado do adaptador: o `postmaster.pid` é lido pelo leitor real, e o `pg_control` são os vinte bytes reais. O "servidor" dos dublês faz o que o de verdade faz, e nada além: uma subida que dá certo escreve o `postmaster.pid` e aparece na tabela de processos. Assim o adaptador nunca tem como saber a porta, o PID ou o estado por outro caminho.
>
> Uma parte da suíte padrão chama o Windows de verdade, sem PostgreSQL e sem rede, como o teste do adaptador de volume já faz. A DACL é aplicada pelo `icacls.exe` real num diretório temporário e lida de volta como SDDL. O lock é tomado por um segundo processo, que é morto segurando-o. A tabela de processos é a do sistema: o próprio teste se procura nela, e um processo filho é encerrado e deixa de estar vivo. E a sonda de porta é exercitada contra um socket de loopback que escuta.
>
> Sob `embedded`, o que não estava na lista: a corrida de §8.2 passo 4 contra um servidor real (a sonda é trocada por uma que sempre responde "livre", com a porta ocupada de fato), um `start()` que chega enquanto o servidor se reinicializa (§8.4), dois processos subindo ao mesmo tempo, e um cluster sob um diretório fora da página de código (§10).

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
| Levar no instalador um `data\` pronto e copiá-lo | Talvez mais rápido que o `initdb` (o custo da cópia era `TBM`; **medido: 2,2 s para 46,4 MB, contra 9,8 s do `initdb.exe`**, §18). Em troca, todas as instalações sairiam com a mesma senha e o mesmo identificador de sistema, e a senha teria de ser trocada em modo single-user antes de abrir a porta. É uma decisão do instalador, e a medição fica registrada para ele |
| `initdb --no-sync` | Mais rápido, mas uma queda de energia logo depois da primeira execução deixaria um cluster com cara de completo. O rename não força os arquivos para o disco. Quanto do `initdb` é `fsync` entra em §18 como diagnóstico, não como opção |
| Senha no Gerenciador de Credenciais ou cifrada com DPAPI | Não protege contra nada que a DACL deixe passar, e a senha precisa chegar em claro à libpq (§7.2). O `keyring` seria uma dependência nova |
| `trust` em loopback | Qualquer conta da máquina conectaria (§7.2) |
| Dois papéis: superusuário e papel do app | Não cria fronteira nova contra quem já tem a senha, e diverge do desenvolvimento (§7.3) |
| Um passo `CREATE EXTENSION` separado | Duplica a migration `999b801e80f4` (§2.3) |
| O Alembic num subprocesso, como no RFC-033a | Um Python a mais em toda subida, e dependente das variáveis de ambiente de §2.2 |
| Rodar `alembic upgrade head` sempre | Idempotente, mas importa o Alembic e roda o `env.py` em toda subida. Comparar os heads custa uma consulta (§7.4; os dois custos eram `TBM`, e estão medidos lá) |
| Listar processos pelo PowerShell | Segundos por chamada (§2.1) |
| `psutil` | Uma dependência nova para três chamadas ao `kernel32` |
| Binários no git, ou no Git LFS | 123 MB no histórico, quando um lockfile de 16 linhas reproduz o mesmo diretório byte a byte |
| Fixar só `postgresql`, `pgvector` e `libpq`, como no RFC-033a | Os outros 13 pacotes, entre eles o OpenSSL e o runtime do Visual C++, flutuariam (§5.1) |
| Recompor o runtime numa árvore própria (`bin\`, `lib\`, `share\`) | O PostgreSQL acha `share\` e `lib\` relativos ao executável, e o layout do conda já funciona, como o RFC-033a provou. Mudar o layout seria um risco sem ganho |

> **Alternativas que só apareceram na implementação (§19).**
>
> | alternativa | por que não |
> | --- | --- |
> | Confiar no código de saída do `pg_ctl start -w` | No Windows ele aceita o arquivo órfão de um servidor que acabou de morrer (§8.3) |
> | Ler só o `startup.log` quando a subida falha | A recusa de `bind` não está lá (§8.1) |
> | Fazer o `status()` tentar uma conexão | Mudaria o que ele é em §4.2: uma leitura de arquivos e da tabela de processos, sem lock e sem senha, barata o bastante para ser chamada à vontade. A espera pelo servidor que se reinicializa ficou no `start()`, e a supervisão é do RFC-036 (§16) |
> | Esperar a saída dos processos pelo PID | Um número de processo não identifica um processo por muito tempo (§8.5) |
> | Encerrar os órfãos sem esperar | Quase sempre eles saem sozinhos em uma fração de segundo (§9.3). Encerrar de imediato trocaria uma espera rara por matar processos à toa em toda recuperação |
> | Recusar todo caminho fora da página de código | O nome curto 8.3 funciona, para o diretório do usuário e para o runtime (§10). Recusar ficou para o volume que não tem nomes curtos |
> | Parar o Docker para medir a suíte | O Docker da máquina é de quem a usa. A prova do alvo foi feita dos dois lados, com ele ligado (§18, §19.2) |

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

> **Como cada risco ficou, depois da medição (§18).**
>
> | risco | como ficou |
> | --- | --- |
> | Caminhos fora da página de código | Confirmado, e tratado pelo segundo ramo (§10) |
> | O runtime do Visual C++ numa máquina limpa | O substituto do teste fechou: toda importação dos quatro binários resolve dentro do runtime ou para o próprio Windows, e um servidor em execução não carregou nada de outro lugar. A máquina limpa continua sem ser testada |
> | Antivírus e SmartScreen | Todas as medições rodaram com a proteção em tempo real do Defender ligada, inclusive a de executáveis nunca vistos, em cópias do runtime sob diretórios novos. Nada foi bloqueado. O rename de §7.1 ganhou novas tentativas por precaução, sem que se tenha medido se alguma chegou a ser usada |
> | Desenvolvimento e app instalado dividem o `postgres\` | A recusa foi medida: com o cluster de pé a partir de uma cópia do runtime, o adaptador do runtime original recusou adotá-lo, e a mensagem traz o caminho do `postgres.exe` que o segura |
> | O job object do `pg_ctl` | Continua em aberto. Não foi medido |
> | O logoff | Não foi feito (§19.3) |
> | A falha de `bind` reconhecida pelo texto | O texto é o esperado, e foi lido de um servidor de verdade. O que estava errado era **onde** procurá-lo (§8.1) |
> | O `initdb` variou sem causa medida | Continua variando, e a causa continua sem medição. Nesta máquina o `initdb.exe` com `fsync` levou de 9,8 a 14,2 s em nove execuções do mesmo dia, e o do script do RFC-033a, 13,4 s e 23,1 s. O `fsync` é cerca de um terço do tempo, mas não explica a variação: a diferença entre duas rodadas iguais é do tamanho do efeito (§18) |
> | Pacotes do lockfile removidos do conda-forge | Não aconteceu: os 16 pacotes foram baixados em 2026-10-11, e o `md5` de cada um conferiu |
> | A suíte quebrar contra o cluster do adaptador | Não quebrou: a mesma contagem contra o cluster do adaptador e contra o Docker (§18) |

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

> **Corrigido e completado pela implementação (§19).** O que a medição mudou no que se passa adiante.
>
> **Para o RFC-034:**
>
> - O `start()` devolve a conexão com a senha fora do `repr`. Quem a puser num `Settings` precisa manter isso, ou a senha aparece no primeiro log que imprimir a configuração.
> - As conexões do adaptador ignoram `PGOPTIONS`, `PGSSLMODE` e `PGGSSENCMODE` de quem chama (§7.4). A engine do projeto **não**: ela é construída de `settings.database_url`, e a libpq continua lendo o ambiente. Um app instalado herda o ambiente do usuário.
>
> **Para o RFC-036:**
>
> - **O `status()` não serve sozinho para a supervisão.** A frase acima está errada. Enquanto o servidor se reinicializa depois da morte de um dos seus processos, o `status()` responde `ready` e toda conexão é recusada (§8.4, §18). A supervisão precisa de uma tentativa de conexão. O `verify_login` do adaptador distingue os três casos: aceita, "ainda não" (`ServerNotReadyError`) e recusa.
> - **Uma tentativa de conexão precisa de prazo.** No Windows, o psycopg só desiste de uma porta em que ninguém escuta quando o `connect_timeout` vence; sem ele, a tentativa durou mais de dois minutos. Um socket puro recebe a recusa em dois segundos.
> - **O código de saída do `pg_ctl start -w` não é prova de que o servidor subiu** (§8.3). O launcher deve perguntar ao adaptador, e não ao `pg_ctl`.
> - **O `stop()` só volta quando os processos saíram**, e não só quando o lock sumiu (§8.5). Depois de um `stop()` não há `postgres.exe` deste cluster, que é o critério que o launcher vai ter.
> - **O `run` atravessa um Ctrl+C sem derrubar o servidor**, e para o que ele mesmo subiu (§18). O primeiro Ctrl+C é do comando; o segundo encerra o comando.
> - **No Windows 11, um programa de console criado sem `CREATE_NO_WINDOW` por um processo sem console abre uma janela do Windows Terminal.** Vale para tudo o que o launcher criar, e não só para o banco.
> - A pergunta do job object do `pg_ctl` (§15) continua aberta. Este RFC não a mediu.
> - **De um processo elevado o adaptador funciona, e o servidor não fica com direitos de administrador** (§8.3, §18). Quem quiser conferir isso num servidor de pé precisa ler o grupo Administradores no token dele: o `TokenElevation` responde `True` mesmo no token restrito.
>
> **Para o instalador:**
>
> - O diretório de instalação **não** precisa caber na página de código, desde que o volume tenha nomes 8.3 (§10). Num volume sem eles o adaptador recusa, dizendo qual setting mudar.
> - O runtime do Visual C++ precisa ir junto, e **vai**: o servidor carrega a cópia de `Library\bin`, e não a do `System32` (§2.3). Os 5,0 MB soltos na raiz são as mesmas DLLs outra vez.
> - Do ICU, 34,2 MB são carregados pelo servidor e 12,8 MB não foram (§5.1).
> - **O `psql.exe` do runtime deixa um processo para trás.** Chamado com os padrões da libpq, ele sobe um `ccapiserver.exe` (o cache de credenciais do pacote `krb5`), que continua vivo depois de o cluster parar e segura os arquivos do runtime. Com `PGGSSENCMODE=disable` ele não sobe. O adaptador nunca chama o `psql`. Um instalador que o chame, ou um atualizador que precise trocar o runtime, vai encontrar esse processo.
> - **Trocar o runtime com um cluster de pé não falha sozinho.** O Windows deixa renomear o diretório com o servidor rodando de dentro dele. Quem atualiza precisa parar o cluster antes, e conferir.
> - **Uma pasta criada por um processo elevado tem o grupo Administradores como dono.** Se a ACL dela não nomear o usuário, o `initdb` chamado por esse mesmo processo não entra nela (§8.3). Vale para qualquer diretório que o instalador crie para o app usar. E um cluster criado pelo app aberto ainda elevado (§2.4), e usado depois sem elevação, não foi exercitado (§19.3).

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

> **Entregue como listado, com um arquivo a mais (§19).** O `.env.example` ganhou os dois settings novos, comentados, porque ele documenta cada setting do projeto. Dentro do adaptador, os módulos são os sete de §4.1 mais `errors.py` e `code_page.py`. Os testes são dez arquivos: nove na suíte padrão (o runtime, o `postmaster.pid`, o diretório do usuário com a DACL e o lock, a sonda de porta, a tabela de processos, a página de código, a preparação do banco, a máquina de estados de §9.1 e o CLI) e um sob o marcador `embedded`. A afirmação de que nada mais muda foi conferida pelo `git status`: os arquivos modificados são os da tabela, mais o `.env.example`.

## 18. Validação

Na máquina do RFC-033a, com o Docker desligado, salvo onde a linha diz outra coisa.

**A regra de não regressão, declarada antes da medição:** nenhuma parcela que o RFC-033a mediu pode piorar além da variação que o próprio RFC-033a registrou. Os limites são o `initdb` de 24,7 a 38,9 s, o `listening → ready` de 0,216 a 0,235 s e o `alembic upgrade head`, com a cadeia inteira, de 2,4 a 2,5 s.

> **Medido em 2026-10-11, noutra máquina, com o Docker ligado (§19.2).** Intel Family 6 Model 126, 8 threads, Windows 11 Home (10.0.26200), Python 3.12.5, disco único `KINGSTON SNV2S1000G` (SSD NVMe), página de código 1252, proteção em tempo real do Defender ligada, processo sem elevação.
>
> **A regra de não regressão não pôde ser aplicada como foi escrita.** Os limites dela são da máquina do RFC-033a, com HDD, e esta é outra. Para ter a linha de base desta máquina, o script do RFC-033a foi rodado aqui, sem mudança, duas vezes no mesmo dia: às 12h06, e às 17h15, logo antes da execução final. A máquina variou quase duas vezes entre uma e outra.
>
> | parcela | script do RFC-033a, 12h06 | script do RFC-033a, 17h15 | adaptador |
> | --- | --- | --- | --- |
> | `initdb` | 13,4 s | 23,1 s | 9,8 s (10,8 s e 12,1 s nos ensaios) |
> | `listening → ready` | 0,137 s | 0,272 s | 0,155 s na primeira execução; 0,21 s na mediana de dez subidas a quente |
> | migrations até o head | 2,7 s (`alembic upgrade head`, num subprocesso) | 3,9 s | 0,57 s em processo, mais 0,72 s da comparação de heads |
>
> **Nenhuma parcela piorou.** As três ficaram dentro da faixa das duas linhas de base, ou abaixo dela. O `initdb` abaixo da faixa não é mérito do adaptador, que chama o mesmo `initdb.exe` com os mesmos flags: é a variação da máquina. Já as migrations ficaram mais rápidas por uma razão que é dele: rodam em processo, sem pagar a subida de um interpretador. Contra os limites literais do RFC-033a as três também passam, o que diz mais sobre o disco do que sobre o adaptador.
>
> Os números em negrito são os da execução final (`measure_adapter.log`). Onde a linha diz "nos ensaios", são as duas execuções completas anteriores do mesmo script, no mesmo dia, às 16h04 e às 16h40.

| verificação | como | resultado |
| --- | --- | --- |
| Primeira execução | `postgres\` vazio, com o runtime já materializado; `start()` e `prepare()` | **14,0 s** no total. `initdb.exe`: 9,8 s (9,9 s com a DACL, a senha e o rename). Subida: 1,04 s, dos quais 0,89 s no `pg_ctl start -w`, com o servidor de `listening` a `ready` em 0,155 s. `CREATE DATABASE`: 1,49 s. Comparação de heads: 0,72 s. Migrations, em processo, as onze até `6d77379a36a1`: 0,57 s. Fatos: 0,006 s. Nos dois ensaios do mesmo dia o total foi 14,7 s e 16,1 s, e o `initdb.exe` 10,8 s e 12,1 s |
| Quanto do `initdb` é `fsync` | `initdb` com e sem `--no-sync`, como diagnóstico (§13) | **Cerca de um terço, com muito ruído.** Duas rodadas alternadas: 10,5 s e 11,9 s com `fsync`, 7,6 s e 6,4 s sem. Pela média, 37%. Nos ensaios a mesma conta deu 34% e 15%, e num deles uma rodada sem `fsync` (13,2 s) levou mais que as duas com (11,5 s e 13,1 s). Nesta máquina a variação entre rodadas é do tamanho do efeito |
| Copiar um `data\` pronto, para o instalador | Copiar o `data\` de um cluster parado (38 MB) | **2,2 s** (mediana de três; de 1,8 a 2,3 s), com a origem já no cache do sistema. O `data\` copiado é o de um cluster com o banco criado e migrado: 46,4 MB em 1296 arquivos, e não os 38 MB de logo depois do `initdb`. É de 4 a 5 vezes mais rápido que o `initdb` desta máquina. O teste de integração depende do mesmo fato: cada teste recebe uma cópia de um cluster parado, e ela sobe |
| Subida a quente | Cluster parado; do `start()` até a conexão aceita | **1,11 s** (mediana de dez; de 0,76 a 2,09 s). Conferência do runtime: 0,001 s. `pg_ctl start -w`: 0,97 s (de 0,64 a 1,88 s), dentro do qual o servidor leva 0,21 s de `listening` a `ready`. Conexão: 0,135 s. Leitura do `postmaster.pid`: abaixo de 0,001 s. Todo o resto do adaptador (lock, observação, `pg_control`, sonda de porta, `solidvision.conf`, `startup.log`): 0,012 s. A comparação de heads não faz parte do `start()`: é o `prepare()`, 0,39 s no mesmo processo. **O `pg_ctl` é 87% da subida, e o servidor é um quinto do `pg_ctl`.** O restante do tempo do `pg_ctl` não foi decomposto: é o que ele gasta criando o `cmd.exe` e o servidor, e esperando, de 100 em 100 ms segundo §8.3. Nos ensaios a mediana foi 1,28 s e 1,29 s |
| Adoção | Cluster de pé; `start()` | **0,10 s** (mediana de vinte; de 0,08 a 0,32 s). É quase só a conexão que confere a senha. Nos ensaios, 0,18 s e 0,08 s |
| Migrations numa subida sem nada a migrar | `upgrade head` sem nada a fazer, contra a comparação de heads | **1,04 s contra 0,49 s.** Cada um medido cinco vezes, num processo novo a cada vez, com a conexão já aberta. `upgrade head` sem nada a fazer: 1,04 s na mediana (de 0,81 a 1,16 s). Comparação de heads, que é o `ScriptDirectory` mais uma consulta: 0,49 s (de 0,38 a 0,84 s). Comparar antes economiza 0,55 s por subida. Nos ensaios, 0,85 s contra 0,51 s e 0,57 s contra 0,28 s: a economia ficou entre 0,29 e 0,55 s, e a comparação custou sempre cerca de metade do `upgrade` |
| Parada | `stop()`, modo `fast` | **0,29 s** (mediana de dez; de 0,22 a 0,72 s), dos quais 0,22 s no `pg_ctl stop`. **Nenhum `postgres.exe` do cluster ao final**, conferido pelos sete PIDs anotados antes da parada. No instante em que o `pg_ctl stop -w` volta, de 0 a 1 dos sete processos ainda existe, e todos somem em 0,007 s na mediana (0,182 s no pior caso); nos ensaios, até 2 dos sete, e 0,134 s no pior caso. O `stop()` espera por eles (§8.5) |
| Memória ociosa | Soma do working set dos `postgres.exe` com o cluster parado em `ready` | **213 MB de working set somado; 26,5 MB de memória privada.** Sete `postgres.exe` (o postmaster e seis filhos), em `ready`, 5 s ociosos e sem cliente. A soma dos working sets conta a memória compartilhada uma vez em cada processo, e por isso varia com o que cada um já tocou: 213 MB aqui, 286 MB e 325 MB nos ensaios. A memória privada confirmada, que não tem essa contagem repetida, deu 26,5 MB nas três vezes |
| A suíte contra o cluster do adaptador | `run -- python -m pytest`, Docker desligado; porta conferida pelo `settings` num subprocesso e `xact_commit` subindo (RFC-033a §4.2) | **2254 passed, 79 deselected** (eram `TBM`), em 86 s. O `run` devolveu 0 e deixou o cluster parado. São os 2085 do RFC-033a mais os 169 testes novos, e os 58 `slow` mais os 21 `embedded`. **O Docker estava ligado, e não desligado** (§19.2), e por isso o alvo foi provado dos dois lados. Primeiro, `run -- <sonda>`: o `settings` do projeto, num subprocesso, respondeu `127.0.0.1:63587`, e o servidor alcançado por ele respondeu porta 63587 e *"PostgreSQL 16.15, compiled by Visual C++ build 1944, 64-bit"*. Depois, os contadores. No cluster do adaptador, `xact_commit` foi de 6 a 324 (+318) e `xact_rollback` de 4 a 271 (+267). No banco do `.env`, que é para onde um nome de variável errado mandaria a suíte, nos mesmos 92 s: +13 commits e **nenhum rollback**. Deixado sozinho por 30 s antes da suíte, esse banco já andava 7 commits, e o rollback é a assinatura da suíte. Nos ensaios, com os testes que existiam em cada momento: 2251 e 2253 passed, nos dois com 79 deselected |
| `pytest -m embedded` | Os testes de integração de §12 | **21 passed, 2312 deselected, em 160 s.** A linha do pytest: *"21 passed, 2312 deselected, 14 warnings in 160.37s (0:02:40)"*. Nos ensaios, os mesmos 21 aprovados, em 236 s e 259 s. O tempo é quase todo de servidores de verdade subindo e parando, e um único `initdb`, o da fixture de sessão |
| Os fatos do cluster | Os campos do passo 8 do RFC-033a, lidos do cluster do adaptador | **Iguais.** Os dez campos foram lidos pelo `read_facts` do próprio script do RFC-033a, importado, e não copiado: versão maior 16, menor 16.15, pgvector 0.8.6, `UTF8`, `libc, C, C`, `hnsw.ef_search` 40, head `6d77379a36a1`, e `Łódź/東京/✈.jpg` de volta byte a byte. O RFC-033a não transcreve as sequências de ordenação e de `lower()`, então elas foram comparadas com o que a localidade `C` define (ordem por bytes, caixa só em ASCII), calculado em Python: idênticas. O servidor continua sem ICU: *"ICU is not supported in this build"* |
| Postmaster morto à força | `taskkill /F` no PID do postmaster, com escrita em andamento; depois `start()` | **Seis de seis: sobe sem intervenção, e nenhuma transação confirmada se perdeu.** Foram 20 522 transações confirmadas ao todo (de 2062 a 4579 por rodada), e todas estavam lá depois. O `start()`, chamado logo depois do `taskkill`, levou **5,2 s** na mediana (de 5,0 a 8,0 s), devolveu `recovered=True` e informou a fase `recuperação`. A recuperação do próprio servidor, de *"database system was interrupted"* a *"ready to accept connections"* no log dele, levou 4,1 s (de 3,7 a 6,4 s). No instante em que o `taskkill` volta, de 3 a 6 dos sete filhos ainda existem; todos saíram sozinhos, e o adaptador não encerrou nenhum. Em duas das seis rodadas o `pg_ctl start -w` devolveu sucesso em 0,28 s e 0,35 s, e o servidor só ficou pronto cerca de 5 s depois: é o defeito de §8.3, e quem esperou foi o adaptador. Nos ensaios, três rodadas cada: de 6,1 a 6,7 s, e de 6,2 a 9,8 s |
| Árvore morta | `taskkill /F /T`, com escrita em andamento | **Seis de seis, com os mesmos critérios.** 29 621 transações confirmadas, todas presentes. `start()`: **4,5 s** na mediana (de 4,0 a 5,8 s). Recuperação do servidor: 3,6 s (de 3,2 a 4,3 s). O `/T` deixa menos para trás: no retorno do `taskkill` havia 0 ou 1 filho vivo, que saiu sozinho. **Num dos ensaios não foi assim.** Numa rodada, dois filhos sobreviveram aos 5 s de espera, o adaptador os encerrou pela regra de §9.3, e o `start()` levou 13,7 s, ainda sem perder transação. Foi uma vez em 27 mortes de árvore ao longo do dia (3 durante o desenvolvimento, 6 nos dois ensaios, 12 numa tentativa de reproduzir e 6 aqui), e a causa não foi identificada (§9.3) |
| Um backend morto | `taskkill /F` em um backend | **O postmaster é o mesmo e continua vivo, mas "a próxima conexão funciona" só vale depois de 5,1 s.** O servidor encerra os outros processos e reinicializa (*"terminating any other active server processes"*, *"all server processes terminated; reinitializing"*): 4,8 s pelo log dele. Nesse intervalo, 60 tentativas de conexão foram recusadas, 56 delas com *"the database system is in recovery mode"*, e o `status()` respondeu `ready` o tempo todo, porque o `postmaster.pid` não é reescrito. As 3285 transações confirmadas antes da morte estavam lá. Nos ensaios a reinicialização levou 13,4 s e 12,4 s. É o que §8.4 passou a esperar |
| `immediate` | `pg_ctl stop -m immediate`; depois `start()` | **Sobe em 5,8 s, com `recovered=True` e as 2190 transações confirmadas.** O `immediate` não deixa `postmaster.pid` para trás, então não é por ele que o adaptador sabe que a parada foi suja: é pelo estado gravado no `pg_control`, lido antes da subida. Nos ensaios, 5,6 s e 6,0 s |
| Órfãos | Tempo de vida dos filhos depois da morte do postmaster; uma isca de outro runtime sobrevive à limpeza | **0,05 s; e as duas iscas sobrevivem.** Com o cluster ocioso, em cinco mortes do postmaster, o último dos seis filhos some 0,049 s depois (mediana; de 0,047 a 0,111 s), e o `startup.log` volta a aceitar escrita em 0,034 s (de 0,027 a 0,101 s). Nos ensaios, 0,071 s e 0,118 s na mediana, e 0,165 s no pior caso. **Para exercitar a limpeza, um filho foi suspenso antes da morte do postmaster**, de modo que não pudesse sair sozinho. Um segundo depois ele continuava lá. O `start()` esperou os 5 s, encerrou só ele (*"Ending PID 13960, a child of the dead postmaster 6292 that outlived the 5 s grace"*) e subiu em 10,4 s, com `recovered=True`. Enquanto isso havia duas iscas de pé: um cluster rodando de uma cópia do runtime em outro diretório, e outro cluster deste mesmo runtime, em outro `data\`. Os sete processos de cada uma continuaram vivos e respondendo a consultas. Um `start()` apontado para o cluster da primeira isca recusa, e a mensagem diz qual runtime o está segurando |
| `initdb` interrompido | Morto aos 2,0 s (era `TBM`); depois `start()` | **Dos dois jeitos; nos dois, `data.initdb` apagado e o cluster recriado.** Com a árvore inteira morta (`taskkill /F /T`), sobra `data.initdb`, sem `data` e sem `initdb.exe`. O `start()` seguinte avisa que está removendo o resto, cria o cluster e sobe em 10,9 s. Com só quem chamou morto (`taskkill /F`), sobra `data.initdb` **e um `initdb.exe` ainda escrevendo nele**. O `start()` seguinte espera esse processo sair, apaga, recria e sobe em 15,1 s. Nos dois casos, `created=True` e nenhum `data.initdb` no fim. Nos ensaios: 19,7 s e 24,8 s num, 16,4 s e 34,4 s no outro |
| Subida morta durante a recuperação | Morta em `starting`; depois `start()` | **A subida seguinte recupera: 5,4 s, `recovered=True`, 600 000 de 600 000 linhas.** Para a recuperação durar o bastante para ser interrompida, o cluster recebeu 600 000 linhas (152 MB de WAL desde o último checkpoint) antes de o postmaster ser morto. O segundo postmaster foi morto com o `postmaster.pid` em `starting`. O `start()` que esperava por ele devolveu um `StartupFailedError` (*"pg_ctl start exited 1"*, seguido do que o servidor registrou), sem tentar de novo. O `start()` seguinte recuperou tudo. Nos ensaios, 7,2 s e 6,7 s |
| Porta ocupada | Um socket escutando na última porta; `start()` | **Sobe em outra porta, em 1,13 s.** A última porta (63587) estava tomada por um socket em escuta, e um segundo `bind` nela é recusado com `WSAEADDRINUSE` (10048). O `start()` escolheu a 59868, e o número é o mesmo nos três lugares: no que o `start()` devolveu, na linha 4 do `postmaster.pid` e no que o próprio servidor responde (`inet_server_port()`). Nos ensaios, 1,04 s e 0,76 s. **A sonda não vê tudo:** a 5432 do `.env`, em que o Docker respondia naquele instante, passa na sonda, porque um `bind` em `127.0.0.1:5432` é aceito (§8.2) |
| Faixa reservada | `netsh … show excludedportrange`; se houver uma faixa, a última porta é forçada para dentro dela | Faixas nesta máquina: **16** (era `TBM`): `5357`, `28385`, `28390`, `49675–49774`, `49775–49874`, `50000–50059`, `50060–50159`, `50160–50259`, `50260–50359`, `50460–50559`, `50560–50659`, `50760–50859`, `50860–50959`, `53988–54087`, `57742–57841` e `58424–58523`. Resultado: **a sonda recusa, e sem a sonda o adaptador reconhece a recusa do servidor.** A porta 49724, no meio de uma faixa, recusa o `bind` com `WSAEACCES` (10013), e não com `WSAEADDRINUSE`. A sonda responde "não serve", e com a última porta forçada para 49724 o `start()` subiu na 61873. Com a sonda desligada de propósito, o servidor foi mandado para a 49724 e registrou *"could not bind IPv4 address "127.0.0.1": Permission denied"* e *"FATAL: could not create any TCP/IP sockets"*, no log do coletor e não no `startup.log` (§8.1). O adaptador leu a recusa (*"Port 49724 refused by bind (attempt 1)"*) e subiu na 61875. E o passo 3: um `bind` na porta 0, repetido 2000 vezes, devolveu 2000 portas distintas, de 59872 a 61872, nenhuma dentro de uma faixa |
| Dois `start()` simultâneos | Dois processos, máquina sem cluster | **Um cria, o outro adota; um único postmaster.** Dois processos disparados juntos contra um diretório vazio. Um devolveu `created=True` e `started_here=True`, em 12,6 s. O outro esperou o lock e devolveu `created=False` e `started_here=False`, em 12,8 s. Os dois receberam a mesma porta (61877) e o mesmo PID de postmaster, e havia sete `postgres.exe`, os de um cluster só. Nos ensaios, 17,0 e 17,2 s, e 15,1 e 15,3 s |
| Processo elevado | `start()` de um PowerShell como administrador | **Funciona, e o token do servidor é o restrito.** Feito à mão, depois da execução final, de um PowerShell elevado pelo aviso do UAC (`measure_adapter.py --only elevated`), duas vezes: às 18h18 e às 18h26. Nas duas, a partir do processo elevado, o adaptador fez uma primeira execução inteira (`initdb`, subida, `CREATE DATABASE`, migrations e parada), depois subiu uma cópia desse cluster, aceitou uma conexão e parou: 12,2 s e 13,7 s. Na segunda os tokens foram lidos. O de quem chama tem o grupo Administradores valendo e 24 privilégios. O do `cmd.exe` que o `pg_ctl` cria, o do postmaster e o de um filho dele têm o grupo só para negar acesso, e 1 privilégio. **O controle foi recusado:** o `postgres.exe` chamado direto pelo mesmo processo elevado, sem o `pg_ctl`, saiu com código 1 e a mensagem que começa em *"Execution of PostgreSQL by a user with administrative permissions is not"*. A afirmação de §8.3 se confirmou, com a prova de que o método a detectaria. As mesmas leituras sem elevação estão em §8.3. **Antes disso a verificação falhou cinco vezes em cinco, por um defeito do script, e não do adaptador.** O `initdb` parava em menos de 1,5 s com *"could not create directory "…/template": File exists"*. O diretório de trabalho do script vem do `tempfile.mkdtemp`, que grava uma ACL protegida de três entradas: o sistema, os administradores e o proprietário. Sem elevação o proprietário é o usuário. Com elevação é o grupo Administradores (`O:BA`, lido nesta execução), e nenhuma entrada nomeia o usuário: um processo do mesmo usuário, sem elevação, não consegue nem listar a pasta. O script passou a conceder esse diretório ao SID do usuário quando roda elevado, e com isso a verificação passou. O log guarda só a execução das 18h26: cada execução sobrescreveu o arquivo, e as cinco saídas com a falha e a das 18h18 não ficaram em log |
| Sem console | `start()` chamado por `pythonw` | **Nenhuma janela de console.** As janelas visíveis de nível superior foram enumeradas antes, durante (a cada 20 ms) e depois. Nos dois ensaios, nenhuma nova. Na execução final apareceu uma, do VS Code que estava aberto na máquina (`Code.exe`, classe `Chrome_WidgetWin_1`): não é um console, nem é de um processo do servidor. **O controle abriu duas janelas de console.** O mesmo `pg_ctl start`, do mesmo `pythonw`, sem o `CREATE_NO_WINDOW`, abriu uma janela do Windows Terminal (classe `CASCADIA_HOSTING_WINDOW_CLASS`), com o caminho do executável no título, e a `PseudoConsoleWindow` que vem com ela. No Windows 11 a janela preta de §2.4 é uma janela do Terminal. A hipótese de §8.3 se confirmou, com a prova de que o método a detectaria |
| Ctrl+C | Ctrl+C no terminal de `run -- <comando longo>` | **O servidor não cai pelo Ctrl+C, e o `run` para o que ele mesmo subiu. O controle caiu.** Três medições, cada uma num console próprio e oculto, com o evento gerado por `GenerateConsoleCtrlEvent`. Na primeira, o servidor é subido pelo adaptador e o Ctrl+C vai para o console de quem chamou: 4 s depois o postmaster está vivo e o `status` responde `ready`. Na segunda, o controle, o `pg_ctl` é chamado sem `CREATE_NO_WINDOW`, e o servidor divide o console: o postmaster morreu, e o `status` responde `stopped`. Na terceira, `run -- <comando que dorme>`: o comando recebeu o Ctrl+C e saiu com 42, o `run` devolveu 42 e disse que parou o cluster, e o `status` depois responde `stopped`. No log do servidor há um único *"received fast shutdown request"*, 0,56 s **depois** de o comando ver o Ctrl+C: quem parou o servidor foi o `run`, e não o evento. Nos ensaios, 0,70 s e 0,19 s |
| Caminhos | Os três casos de §10 | **Funciona, funciona, funciona, com o segundo ramo.** Cada caso é uma primeira execução completa (com `initdb`), um `prepare()`, uma parada, uma subida a quente e uma adoção. `João Silva`: funciona sem tocar no caminho. `Łukasz 東京`: sem o ramo falhava no `initdb` (§10 tem a mensagem), e com ele funciona pelo nome curto. Runtime com espaço: funciona. E um quarto caso, acrescentado: o runtime em `Łukasz 東京 runtime`, que também falhava e também funciona pelo nome curto. Nos quatro, a linha 2 do `postmaster.pid` aponta para o `data\` certo |
| DLLs | Tabela de importação dos quatro binários, resolvida contra o runtime e o Windows; inventário da raiz do ambiente | **Fechado; e a raiz são as DLLs do runtime do Visual C++.** As tabelas de importação e de carga tardia foram lidas por um leitor de PE feito com `struct`, e seguidas até o fim. `postgres.exe`: 23 importações diretas, e no fecho 15 DLLs do runtime, 12 conjuntos de API, 7 `KnownDLLs` e 4 DLLs do Windows. `initdb.exe` e `pg_ctl.exe`: 10 do runtime cada. `vector.dll`: as 15 do `postgres.exe`, mais o `postgres.exe`, de quem ele importa. Nenhuma importação ficou sem resolver, e nenhuma resolve para um arquivo que não seja do runtime nem do Windows. "Do Windows" é perguntado ao sistema (proteção de recursos), e não lido do nome do produto, que neste Windows vem traduzido. **Conferido num servidor de verdade:** o postmaster tem 46 módulos carregados, 16 do runtime e 30 de `C:\WINDOWS`, e nenhum de outro lugar; um backend que usou o `vector` tem um a mais, o `vector.dll`. O inventário da raiz está em §2.3 |
| Ambiente do usuário | `PGPORT`, `PGDATA` e `PGOPTIONS` definidos, e uma pasta com outra `libpq.dll` no `PATH` | **Nada muda.** Um processo com `PGPORT=1`, `PGDATA` apontando para onde não há cluster, `PGOPTIONS` de só leitura, `PGPASSWORD` errada, `PGHOST` inválido e, na frente do `PATH`, uma pasta com cinco DLLs falsas (`libpq`, `libssl`, `libcrypto`, `zlib`, `vcruntime140`) fez `start()` e `prepare()` sem erro. O servidor subiu do `data\` certo, na porta que o adaptador escolheu, e o postmaster não carregou nenhum módulo da pasta falsa. O `CREATE DATABASE` sob um `PGOPTIONS` de só leitura é coberto pelo teste de integração, que parte de um cluster sem banco: o banco foi criado, o que só acontece se as conexões do adaptador ignoram a variável (§7.4) |
| DACL | Lida por SID | **Só o usuário e `S-1-5-18`.** Lida como SDDL, que nomeia contas por SID: `postgres\` tem `D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;<SID do usuário>)`. O `P` é a herança cortada, e não há terceira entrada: os administradores saíram. `16\password` e `16\data` têm as mesmas duas entradas, herdadas (`ID`) |
| A senha não vaza | Busca pela senha nos logs do app, no log do servidor e na saída do CLI | **Nenhuma ocorrência.** As 8 senhas geradas nesta execução foram procuradas, cada uma, em 278 lugares: o log do app, 55 logs de servidor, 110 arquivos `startup.log` e `pg_ctl.log`, 110 arquivos de configuração, a saída capturada do CLI (1191 caracteres) e a saída do próprio script. Nos ensaios, 8 senhas e nenhuma ocorrência, em 218 e 243 lugares |
| Firewall | Primeira subida numa sessão em que o Defender Firewall está ligado | **Nenhum diálogo, e nenhuma regra criada.** Com os três perfis do Defender Firewall ligados, um `postgres.exe` foi subido de um caminho que nunca tinha rodado nesta máquina (uma cópia nova do runtime), escutando só em `127.0.0.1`. Nos 3 s seguintes nenhuma janela apareceu na área de trabalho, e entre as janelas vistas aqui e em *Sem console* nenhuma é um diálogo ou um alerta de segurança. Depois, a listagem de regras do firewall (14 117 linhas, lida sem elevação) não tem regra com o caminho deste runtime, nem das cópias, nem de nenhum `postgres.exe`. O que não foi feito é o que a linha pede ao pé da letra, a primeira subida de uma **sessão** nova do Windows: o substituto foi a cópia nova do runtime, um executável num caminho para o qual o firewall não tinha regra nem decisão anterior |
| Banco mais novo que o código | `alembic_version` com uma revisão desconhecida | **Recusa, sem escrever.** A mensagem: *"The database is at revision 0123456789ab, which this code does not know: a newer SolidVision migrated it. Running this code against a schema it does not know is worse than not starting."* Depois da recusa, `alembic_version` continua com a revisão desconhecida, e os contadores de linhas inseridas, atualizadas e apagadas do banco não se moveram (0/0/0) |
| Outra versão maior | `PG_VERSION` forjado como `15` | **Recusa, sem tocar em nada.** O `data\` ficou com as mesmas 1323 entradas, mesmos nomes e mesmas datas de modificação, e nenhum `data.initdb` foi criado. O `status()` responde `other version` |
| Logoff | Logoff do Windows com o cluster de pé; nova sessão; `start()` (manual) | **Não executado.** É manual por natureza, e encerraria a sessão em que a implementação estava sendo feita. O script imprime os quatro passos. O que se espera é o que as três mortes forçadas acima mostraram: `status` responde `stopped`, e o `start` seguinte avisa que recuperou |
| Estilo e tipos | `black --check`, `ruff check`, `mypy` | **Zero erros no código novo.** `black --check` e `ruff check` limpos sobre o adaptador, os testes, o `settings.py`, o `vendor_postgres.py` e o `measure_adapter.py`. `mypy` limpo sobre os três primeiros, que são o que a configuração do projeto manda verificar (`backend/`). Duas ressalvas, as duas em §19: o `black` foi rodado por outro interpretador, porque se recusa a rodar no Python 3.12.5 desta máquina; e o `mypy` do projeto inteiro acusa 5 erros, todos em arquivos que este RFC não toca |
| Docker continua funcionando | `pytest` puro, com o Docker ligado | **2254 passed, 79 deselected, em 98 s: a mesma contagem da suíte contra o cluster do adaptador**, e a de antes somada aos testes novos (2085 + 169, e 58 + 21). É o `pytest` puro, sem nenhuma variável do adaptador no ambiente, e que por isso lê o banco do `.env`. O contêiner desta máquina é `pgvector/pgvector:pg17`, e não o 16 do `docker-compose.yml` (§19.2) |

## 19. Correções feitas durante a implementação

Registradas à vista do texto original, como o RFC-033a fez com as suas. Nenhuma muda uma decisão de §3. Três mudaram **código** que o documento descrevia de outro jeito, e vêm primeiro.

### 19.1 O que a medição contrariou

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §8.1, "Dois logs" | O coletor *"só começa depois que os sockets já foram abertos"*, e por isso *"a causa de uma falha de subida, como um `bind` recusado, iria para o `startup.log`"* (hipótese) | **Falsa.** No PostgreSQL 16 o coletor sobe antes dos sockets. A recusa de `bind` está no arquivo do coletor, e o `startup.log` de uma subida tem só as duas linhas do redirecionamento. O adaptador lê os dois, e do coletor só o que foi escrito na subida em curso. Como estava descrito, nenhuma falha de `bind` seria reconhecida, e o passo 4 de §8.2 nunca aconteceria |
| §8.3 | *"O `pg_ctl` […] espera o servidor ficar pronto lendo o `postmaster.pid`"* | **Verdade, e insuficiente.** No Windows o `pg_ctl` sobe um `cmd.exe`, e não tem como casar o `postmaster.pid` com o processo que criou. Ele aceita um arquivo com poucos segundos de idade, inclusive o arquivo órfão de um servidor que acabou de morrer, que ainda diz `ready`. Medido em §18: sucesso em menos de meio segundo, com o login recusado. O adaptador confere por conta própria que o arquivo é de um postmaster vivo, deste runtime, neste `data\` |
| §8.4 e §16 | A adoção exige que *"uma conexão com a senha do `password`"* seja aceita, e *"o `status()` serve para a supervisão. O `pg_isready` não é necessário"* | **O `postmaster.pid` não é reescrito quando o servidor se reinicializa.** Depois da morte de um backend, o servidor recusa conexões por segundos com *"in recovery mode"*, e o arquivo continua dizendo `ready`. O `start()` passou a esperar essa recusa passar. E o `status()`, que só lê o arquivo e a tabela de processos, **não basta** para supervisão: quem supervisiona precisa tentar uma conexão (§16) |
| §9.1 | A subida falha *"com memória compartilhada ainda em uso"* quando os filhos do postmaster morto ainda vivem | **Não chegou a acontecer.** O que os órfãos seguram primeiro é o `startup.log`, que todo processo do servidor herda aberto e que o adaptador reescreve antes de subir. A regra de §9.3 foi aplicada nesse ponto. A linha da memória compartilhada ficou no código, sem ter sido exercitada por um servidor de verdade |
| §8.5 | *"`pg_ctl stop -m fast -w -t 60`"* | **O `pg_ctl stop -w` volta antes de os processos saírem.** Ele espera o `postmaster.pid` sumir. Os processos do servidor ainda existem por alguns milissegundos, e por 0,18 s no pior caso medido (§18). O `stop()` espera por eles, conferindo cada um pela identidade (executável deste runtime, e o postmaster como pai), e não só pelo número do processo (§8.5) |
| §7.1 | Um `data.initdb` é *"sempre resto de uma criação interrompida"* | **Só se a árvore inteira morreu.** Morto sozinho, quem chamou solta o lock e o `initdb.exe` continua. O adaptador espera o `initdb.exe` deste runtime sair antes de apagar o diretório |
| §10 | *"Se o segundo caso funcionar, nada muda. Se falhar, […] usa o nome curto 8.3"* | **Falhou, e o segundo ramo está em vigor**, para o diretório do usuário e também para o runtime. A restrição herdada pelo instalador ficou menor do que a prevista |
| §5.1 | O ICU *"provavelmente não sai"* | **34,2 dos 47,0 MB não saem**: o processo os carrega. Os outros 12,8 MB não foram carregados |
| §2.3 | Não se sabia se o `postgres.exe` achava o runtime do Visual C++ *"porque elas estão no runtime ou porque esta máquina as tem em `System32`"* | **Porque estão no runtime.** A máquina também as tem no `System32`, postas por um redistribuível, e o servidor não as usa |

### 19.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §18, a máquina | **Não é a do RFC-033a.** §18 abria com *"Na máquina do RFC-033a"*, um HDD. A implementação foi feita em outra: Intel Family 6 Model 126, 8 threads, Windows 11 Home, Python 3.12.5, disco único `KINGSTON SNV2S1000G` (SSD NVMe). Contra números de um HDD, um NVMe passa na regra de não regressão sem medir nada. Para a regra continuar dizendo alguma coisa, o script do RFC-033a foi rodado **nesta** máquina, e é contra essas execuções que §18 compara |
| §18, o Docker | **Não foi desligado.** O Docker desta máquina é de uso do desenvolvedor, e este RFC não mexe nele. A suíte contra o cluster do adaptador rodou com o Docker ligado, com a prova do alvo feita **dos dois lados**: os contadores de transação do cluster do adaptador subiram, e os do banco do `.env` não tiveram rollbacks, que são a assinatura da suíte. O critério do roadmap, *"com o Docker desligado"*, **não foi reproduzido ao pé da letra** |
| §18, o Docker desta máquina | O contêiner em execução aqui é `pgvector/pgvector:pg17`, sobre o volume antigo. A troca para `0.8.6-pg16` do RFC-033a §7 foi feita na outra máquina e nunca aplicada nesta: o `docker-compose.yml` já está certo, e o contêiner é anterior a ele. A linha *"Docker continua funcionando"* foi medida contra esse 17 |
| §18, a suíte | `test_resolving_the_embedding_model_downloads_nothing` falha nesta máquina contra qualquer banco, porque o `.env` local tem `WARM_UP_MODELS=true` (é como o `.env.example` vem) e o `TestClient` carrega o modelo. As suítes de §18 rodaram com `WARM_UP_MODELS=false` no ambiente, e o log registra isso nas condições |
| §18, o processo elevado | **Feito à mão, depois da execução final, e o script precisou de conserto antes de medir o adaptador.** O diretório de trabalho do script, criado por `tempfile.mkdtemp`, sai de um processo elevado com o grupo Administradores como dono e sem entrada para o usuário, e o `initdb` não entra nele. Quando roda elevado, o script agora concede o diretório ao SID do usuário e imprime o descritor que a pasta tinha. Ele também passou a ler, do token de quem chama e do token do servidor, o estado do grupo Administradores e a contagem de privilégios, e a rodar o `postgres.exe` direto como controle. Sem elevação ele agora faz as mesmas leituras, para a comparação, onde antes só imprimia as instruções. É a única verificação que mudou depois da execução final |
| §4.1 | Dois módulos a mais, `errors.py` e `code_page.py` |
| §4.2 | Sete respostas no `status()`, e não cinco; `on_phase` também no `prepare()` |
| §5.1 | O lockfile foi gerado de novo (`vendor_postgres.py --lock`), porque o log do RFC-033a não está no git. O `md5` é conferido pelo próprio script. O `sha256` do manifesto ignora o fim de linha. E o script recusa trocar um runtime em uso: o Windows deixa **renomear** o diretório com um servidor rodando de dentro dele, então o teste é tentar abrir cada executável para escrita, o que o Windows não deixa com o programa em execução |
| §6 | A DACL é aplicada no caminho da primeira execução, e não só no `mkdir`. Um quinto arquivo, `pg_ctl.log` |
| §7.4 | `pg_advisory_xact_lock` para dois chamadores migrando juntos; `42P04` tolerado no `CREATE DATABASE`; `options`, `sslmode` e `gssencmode` explícitos nas conexões do adaptador |
| §8.2 | A sonda pede a porta com exclusividade (`SO_EXCLUSIVEADDRUSE`). Um "não" errado custa um número de porta; um "sim" errado custa uma subida que falha e é repetida |
| §12 | Os arquivos do cluster não são dublês nos testes unitários: são o estado |
| §17 | O `.env.example` também mudou |
| §18 | Verificações a mais do que a tabela pedia, cada uma para sustentar uma frase deste documento: o que o `pg_ctl start -w` responde sobre um arquivo órfão, o que o `psql.exe` do runtime deixa para trás, o runtime num diretório fora da página de código, e, ao fim de tudo, se sobrou algum processo. Os resultados delas estão nas notas das seções a que servem (§8.3, §16, §10) e no log |
| §18, o `black` | O `black` se recusa a rodar no Python 3.12.5, que é o desta máquina, por um defeito conhecido dessa versão. Ele foi rodado, na mesma versão do projeto (26.5.1), por outro interpretador |
| §18, o `mypy` | Zero erros no código novo. O `mypy` do projeto inteiro acusa 5 erros em 4 arquivos que este RFC não toca (dois repositórios e dois testes anteriores), com as versões de SQLAlchemy e mypy instaladas nesta máquina |

### 19.3 O que ficou sem medição

Dito aqui para não parecer medido em outro lugar.

- **Um cluster criado por um processo elevado e usado depois sem elevação** (§8.3, §18). Não foi exercitado. O que foi lido é que uma pasta criada pelo próprio processo elevado tem o grupo Administradores como dono. De quem são os arquivos que o `initdb` e o servidor criam debaixo dela, não.
- **O logoff** (§9.4, §18).
- **O Docker desligado**, ao pé da letra (§18).
- **Um volume sem nomes 8.3** (§10). O ramo da recusa é coberto por um teste unitário, e não por um volume de verdade.
- **Uma máquina limpa, sem o runtime do Visual C++** (§15). O fecho de DLLs de §18 mostra que o servidor não precisa do `System32` para isso, o que é o que o risco pedia como substituto.
- **O HDD.** Todos os tempos de §18 são de um NVMe. Os tempos de espera do adaptador foram escolhidos com folga sobre eles e sobre os do RFC-033a, e é no HDD que uma folga errada apareceria. O script roda lá do mesmo jeito.
