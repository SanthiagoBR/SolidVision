# RFC-030 — Acesso ao Arquivo: caminho, thumbnail e revelação

**Status:** Implementado
**Depende de:** RFC-025 (busca), RFC-026 (camada HTTP), RFC-027 (dispositivos e ponto de montagem), RFC-028 (data de captura), RFC-029 (jobs)
**Migration:** sim — `images.thumbnail_path` (`f4b9e2d7c615`)
**Medição:** `experiments/rfc-030-file-access/` — `measure_thumbnail_cost.py` (§7.2, §9), `measure_reveal_and_revalidation.py` (§4.2, §6, §7.2, §12) e `verify_explorer_select.ps1` (§5.1), cada um com o `.log` ao lado; `baseline.log` guarda a linha de base anterior à primeira linha de código

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*
> durante a implementação e devia ser escrito de volta aqui depois. **Isso foi
> feito:** não resta nenhum `TBM`, e cada número abaixo vem com a escala e as
> condições em que foi medido. O que a medição não cobriu — leitura fria de
> disco mecânico externo, e o `explorer.exe` lançado dentro do script de
> latência — está dito onde aparece, em vez de preenchido por dedução.

> **Quatro afirmações deste documento estavam erradas contra o código ou contra
> a plataforma, e a implementação as corrigiu.** São §7.2 (a thumbnail "de
> graça, da imagem já decodificada", repetida na tabela de §3), §7.2 de novo
> (`max-age=31536000` ao lado do `ETag` reintroduz o erro que a própria seção
> corrigia), §5.1 (a forma do argumento `/select,`, que abre a pasta errada
> quando o caminho tem espaço, e `subprocess.run`) e §11 (onde mora a porta de
> revelação; `ARCHITECTURE.md` "ganhando" uma coluna que já documentava). Cada
> uma está marcada **Correção** na seção correspondente, com a frase original
> preservada à vista — a convenção do [README](README.md).
>
> **Duas foram correções no código existente, não nesta RFC**, e estão marcadas
> **Registro**: `ImageNotFoundError` estava numa base que responde 400 (§4.2), e
> não havia base de erro que respondesse 410 (§5.1).
>
> **Sobre a numeração.** A docstring de `search_schema.py`, escrita pelo RFC-026, promete *"o `GET /images/{id}` que o RFC-027 vai acrescentar"*. O RFC-027 acabou sendo dispositivos e identidade de volume, porque a janela para reescrever a chave primária barato estava fechando (RFC-027 §6.2). Este é o RFC que cumpre aquela promessa, três números depois. O registro fica aqui em vez de a docstring ser corrigida em silêncio, seguindo a lição do RFC-012 — e a docstring reescrita cita a frase antiga em vez de apagá-la.

---

## 1. Contexto

O RFC-026 decidiu, deliberadamente, não devolver o caminho do arquivo:

> **`search_schema.py`:** *"`path` está deliberadamente ausente ainda que `SearchHit.image` carregue um. Publicá-lo vazaria o layout do sistema de arquivos do servidor para todo chamador, e entregaria aos clientes um identificador que muda sempre que um arquivo se move."*

O contra-argumento chegou como uma frase, e a frase está certa:

> *"Mostrar o caminho bruto é basicamente o coração do projeto. Não faz o menor sentido fazer todo um projeto de encontrar arquivos se não ajudar o usuário a acessar ele no final do processo."*

O `ARCHITECTURE.md` §1 descreve o sistema como *"recuperação de imagens"*. Recuperação que termina em um `filename` sem extensão e um UUID não recuperou nada: o usuário sabe que a foto existe e continua sem saber onde ela está. O último passo do fluxo está faltando, e ele é o passo que o produto promete.

## 2. Problema

O RFC-026 deu **dois** argumentos contra publicar o caminho. Eles têm destinos diferentes, e tratá-los como um só é o erro que este RFC precisa evitar.

### 2.1 Primeiro argumento: dissolve

> *"Vazaria o layout do sistema de arquivos do servidor para todo chamador."*

Correto para um servidor remoto multi-inquilino. O SolidVision não é um. O `README.md` define a premissa: *"os arquivos permanecem em seus locais originais sem necessidade de upload"*. O `ARCHITECTURE.md` §2 descreve um usuário indexando os próprios discos. E o próprio código já sabe disso — `routers/images.py`, ao justificar por que não loga a consulta:

> *"o texto é o histórico de busca de um usuário, e este é um produto local-first cuja premissa é que ele continue sendo dele."*

**O sistema de arquivos do servidor é o sistema de arquivos do usuário.** Devolver o caminho é devolver a ele um dado que já é dele, sobre arquivos que ele mesmo mandou indexar. A premissa do argumento não vale nesta implantação, então não há decisão a reverter — há uma condição a declarar, e a declaração precisa ser executável e não uma nota de rodapé (§6).

A premissa volta a valer no instante em que alguém sobe a API em `0.0.0.0`. É por isso que §6 é um guarda e não um parágrafo.

### 2.2 Segundo argumento: continua valendo, e molda o desenho

> *"Entregaria aos clientes um identificador que muda sempre que um arquivo se move — o RFC-024 deriva o id da imagem do caminho, então um caminho armazenado é ao mesmo tempo instável e inutilizável."*

Isto **não** dissolve. Continua verdadeiro, e o RFC-027 o tornou mais verdadeiro ainda: o caminho absoluto não existe mais no banco. Ele é computado juntando o ponto de montagem — resolvido em tempo de requisição, porque letra de unidade não é identidade — ao `relative_path`.

A conclusão que se preserva: **`id` é o identificador; o caminho é uma afordância de exibição e de ação.** Um cliente que guarde o caminho e o use depois para pedir alguma coisa ao servidor está errado, e a API não deve aceitar caminho em lugar nenhum — o que §5 transforma em propriedade de segurança em vez de convenção.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Caminho na resposta de busca | **Sim** — como exibição, nunca como identificador (§4) |
| Forma do caminho | Absoluto quando o dispositivo está conectado; `null` + rótulo do dispositivo quando não (§4.1) |
| `GET /api/v1/images/{id}` | Metadados completos de uma imagem — a promessa do RFC-026 (§4.2) |
| `GET /api/v1/images/{id}/thumbnail` | Bytes, de um cache gerenciado pelo app (§7) |
| `POST /api/v1/images/{id}/reveal` | Abre o gerenciador de arquivos com o arquivo selecionado (§5) |
| Entrada de qualquer rota | **Sempre `id`, nunca caminho** (§5.1) |
| Onde ficam as thumbnails | Diretório do app, **nunca no acervo** (§7.1) |
| Quando são geradas | Durante a indexação, ~~com a imagem já decodificada~~ **com uma decodificação própria, só para imagens que ganharam embedding** (§7.2, corrigido) |
| Ações locais | Atrás de `settings.allow_local_file_actions` **e** de um guarda de loopback (§6) |
| Plataforma | Windows neste RFC; portas declaradas (§5.3) |
| Migration | `images.thumbnail_path` |

## 4. O caminho na resposta

```json
{
  "id": "…",
  "filename": "DJI_0042",
  "similarity": 0.3117,
  "captured_at": "2018-06-12T14:30:00",
  "capture_source": "exif_original",
  "device": { "id": "…", "label": "HD3", "connected": false },
  "relative_path": "fotos/2018/junho/DJI_0042.JPG",
  "absolute_path": null
}
```

`relative_path` está **sempre** presente: é fato armazenado, independe de qualquer disco estar plugado, e já diz ao usuário a maior parte do que ele quer saber. `absolute_path` é derivado e só existe quando há ponto de montagem para derivá-lo.

**Implementação.** `Device` não tem campo `connected` — deliberadamente, desde o RFC-027, porque seria falso em toda leitura feita depois de o usuário puxar o cabo. `device.connected` é, portanto, **perguntado ao sistema operacional por requisição** e nunca lido de uma coluna. Quem pergunta é `ResolveImageLocationUseCase` (`application/use_cases/resolve_image_location.py`), uma use case pequena que combina `DeviceRepository` e `DeviceLocator` — o mesmo par que `CreateIndexingJobUseCase` já combinava. Não mora na rota (é regra, e Presentation não tem regra) nem dentro de `SearchImagesUseCase` (onde um arquivo está não tem nada a ver com o quanto ele casou com a consulta, e `GET /images/{id}` precisa da mesma resposta sem busca nenhuma).

**Uma enumeração por disco distinto na página, não uma por resultado.** `MountedDeviceLocator.mount_point()` enumera os volumes montados a cada chamada e não guarda cache, por desenho (RFC-027 §7). A use case agrupa os resultados por `device_id` dentro da requisição e pergunta uma vez por grupo; o agrupamento morre com a requisição, de modo que um disco desplugado entre duas buscas é visto como desplugado. Medido: `mounted_volumes()` custa **0,19 ms** de mediana nesta máquina, com um volume montado (`measure_reveal_and_revalidation.log`, parte 3); um teste fixa que dez resultados em um disco fazem exatamente uma enumeração.

Os caminhos saem pelo `ImagePath.__str__`, com `/` em qualquer plataforma: um cliente JSON não deveria precisar saber qual sistema operacional respondeu para quebrar um caminho em partes.

### 4.1 O disco desconectado é a resposta, não a falha

Este é o comportamento que o RFC-027 §2.3 antecipou e que só se materializa aqui.

| situação | o que a resposta diz | o que o usuário faz |
| --- | --- | --- |
| HD3 plugado | `connected: true`, `absolute_path: "F:/fotos/2018/…"` | clica e abre |
| HD3 na gaveta | `connected: false`, `absolute_path: null`, `device.label: "HD3"` | **pega o HD3** |

A segunda linha não é degradação. Para alguém com vinte discos, *"está no HD3, na pasta 2018/junho"* é a resposta completa — o sistema resolveu o problema de "em qual dos vinte?", que é o problema caro. Uma API que devolvesse 404 ou um caminho quebrado teria transformado a resposta certa em erro.

E é por isso que as thumbnails de §7 importam mais do que o argumento de tamanho de payload sugere: com o disco desconectado, a thumbnail em cache é a única forma de o usuário **ver** a foto que ele acabou de encontrar e confirmar que é aquela antes de ir buscar o disco.

### 4.2 `GET /api/v1/images/{id}`

A rota que o RFC-026 prometeu. Devolve o mesmo objeto de §4 sem `similarity` — que não existe fora de uma consulta, pela razão que o RFC-025 §4.1 deu para `SearchHit` não ser um campo de `Image`.

404 quando o id não existe. **200 quando o dispositivo está desconectado** — a linha existe, e é isso que a rota descreve.

> **Registro (código existente).** `ImageNotFoundError` já existia, herdando
> direto de `DomainError` — que o `error_handlers.py` responde com **400**. Ela
> nunca tinha sido levantada em código de produção: só era importada e testada
> por `issubclass`. Foi re-baseada para `NotFoundError`, o mesmo movimento que o
> RFC-029 fez com `DeviceNotFoundError`, e seguro pelo mesmo motivo — nenhum
> cliente jamais recebeu um status por ela. `test_error_handlers.py` a nomeia
> como a única reclassificação deste RFC.
>
> O mesmo teste ficou mais estrito no caminho. Ele calculava o status esperado
> de cada erro a partir da própria hierarquia (`404 if issubclass(...,
> NotFoundError)`), o que o fazia passar para *qualquer* re-baseamento —
> inclusive um acidental. As expectativas agora estão escritas: tudo que não
> foi reclassificado de propósito ou nasceu sob uma base com status continua
> 400.

## 5. Revelar o arquivo

Mostrar `F:/fotos/2018/junho/DJI_0042.JPG` como texto é a forma mais fraca de atender §1. O que o fotógrafo quer é a pasta abrindo com o arquivo selecionado.

```
POST /api/v1/images/{id}/reveal   →   204 No Content
```

### 5.1 A propriedade de segurança: nenhum caminho entra

O corpo da requisição é **vazio**. O cliente manda um UUID na URL; o servidor busca a linha, resolve o ponto de montagem do dispositivo e monta o caminho ele mesmo.

Isso não é estilo. É o que torna uma classe inteira de ataque impossível em vez de filtrada: não existe string de caminho vinda do cliente para sanitizar, então não existe travessia de diretório, não existe `..`, não existe caminho UNC apontando para uma máquina remota. O único parâmetro é um UUID, e um UUID que não está na tabela é um 404.

A propriedade é fixada por testes de assinatura, não por convenção: o handler recebe `image_id` e a use case e nada mais; o *dependant* da rota não tem parâmetro de query, header, cookie ou corpo, nem nas dependências; o OpenAPI publicado não tem `requestBody`; e uma requisição com `{"path": "\\\\attacker\\share\\evil.exe"}` no corpo e `?path=C:/Windows/System32/cmd.exe` na query revela o arquivo que o servidor montou e mais nada.

A execução em si nunca passa por um shell:

```python
subprocess.run(["explorer", f"/select,{absolute_path}"], shell=False, check=False)
```

> **Correção (§5.1).** A linha acima está errada em duas coisas, e a primeira
> quebra a funcionalidade exatamente no caso comum.
>
> **`/select,` e o caminho não podem ser um item só da lista.** `subprocess`
> põe aspas num item que contém espaço, então para `fazenda São João.jpg` o
> Explorer recebe `"/select,C:\…\fazenda São João.jpg"` — e o parser do
> Explorer não reconhece a chave dentro das aspas: **abre a pasta Documentos,
> sem nada selecionado.** Como dois itens (`"/select,"` e o caminho), a linha
> de comando vira `/select, "C:\…\fazenda São João.jpg"`, e o arquivo é
> selecionado. Verificado no Windows 10 abrindo cada forma e lendo de volta a
> pasta aberta e o item selecionado pelo objeto COM `Shell.Application`
> (`verify_explorer_select.log`):
>
> | forma | caminho sem espaço | caminho com espaço e acento |
> | --- | --- | --- |
> | um item, `"/select,<caminho>"` | pasta certa, arquivo selecionado | **abre Documentos, nada selecionado** |
> | dois itens, `"/select,"` + caminho | pasta certa, arquivo selecionado | pasta certa, arquivo selecionado |
> | `WindowsFileRevealer` real, via `Popen` | pasta certa, arquivo selecionado | pasta certa, arquivo selecionado |
>
> As duas primeiras execuções do script leram "nada selecionado" em casos que
> estavam certos: a pasta é conhecida assim que a janela existe, a seleção é
> aplicada depois, e uma leitura cedo demais a perde. O script final relata as
> duas coisas separadamente e espera até 20 s pela seleção; o resultado acima é
> dessa execução, e o caso com defeito errou a **pasta**, que não depende de
> tempo. `test_file_revealer.py` fixa a linha de comando exata que
> `CreateProcess` recebe.
>
> **`Popen`, não `run`.** `run` espera o processo terminar. Com a opção do
> Windows "abrir janelas de pasta em um processo separado", esse processo é a
> própria janela, e a requisição ficaria pendurada até o usuário fechá-la. O
> `check=False` estava certo pelo motivo dado abaixo; com `Popen` ele deixa de
> ser necessário, porque o código de saída nem é esperado.
>
> **E um endurecimento que a linha não tinha:** `explorer.exe` é chamado pelo
> caminho absoluto, sob `%SystemRoot%`. Com `shell=False`, o Windows procura um
> executável sem caminho no diretório do `python.exe` e no diretório corrente
> *antes* dos diretórios do sistema — um `explorer.exe` perdido na pasta
> `Scripts` de um virtualenv seria o que rodaria.
>
> A implementação:
>
> ```python
> subprocess.Popen(
>     [r"%SystemRoot%\explorer.exe", "/select,", absolute_path],
>     shell=False, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
> )
> ```

`shell=False` com lista de argumentos. Mesmo que o caminho contivesse metacaracteres — e ele vem do disco do próprio usuário, não da rede — não há shell para interpretá-los.

`explorer.exe` retorna código de saída não zero mesmo em sucesso, um comportamento conhecido dele; daí `check=False`, e o resultado é 204 quando o processo foi lançado, não quando ele "deu certo".

Um ponto que a linha não previa: **o Explorer não falha para um arquivo inexistente** — ele abre uma pasta padrão em silêncio. O adaptador confere que há um arquivo no caminho antes de lançar, e levanta `FileNotFoundError` como qualquer E/S levantaria; a use case traduz isso no erro de domínio abaixo.

Erros de domínio: dispositivo desconectado → 409 (o pedido é legítimo, o estado não permite); arquivo ausente apesar do disco conectado → 410 (a linha existe, o arquivo não existe mais).

> **Registro (código existente).** O 409 já tinha para onde ir:
> `DeviceNotConnectedError` é `ConflictError` desde o RFC-029, e é exatamente o
> que `Image.require_absolute_path()` levanta — `RevealImageUseCase` o reaproveita
> em vez de escrever uma checagem nova, e só reescreve a mensagem para nomear o
> disco pelo rótulo que o usuário deu ("plugue o HD3", não "plugue o
> 564e9201-…"). **O 410 não tinha.** `error_handlers.py` conhecia 404 e 409 e
> nada mais. Ganhou uma terceira base, `GoneError`, ao lado de `NotFoundError`
> e `ConflictError` em `domain_error.py`, e `FileGoneError`
> (`exceptions/file_access_errors.py`) herda dela. A distinção entre as três é
> se esperar ajuda: um id que nunca existiu é 404; um disco na gaveta é 409,
> porque o mesmo pedido passa com o disco plugado; um arquivo apagado de um
> disco plugado não volta sozinho, e dizer ao cliente para tentar de novo seria
> dizer algo falso.

### 5.2 Por que é `POST`

`GET` é seguro e idempotente por contrato. Isto abre uma janela na máquina do usuário — um efeito colateral no mundo físico. Além do princípio: um `GET` seria pré-buscado por qualquer coisa que percorra links, e uma UI com dez resultados abriria dez janelas do Explorer sem ninguém ter clicado.

### 5.3 Plataforma

| SO | comando |
| --- | --- |
| Windows | `explorer /select, <path>` — **entregue** (a forma corrigida de §5.1) |
| macOS | `open -R <path>` — declarado, não entregue |
| Linux | `dbus-send` para `org.freedesktop.FileManager1`, com `xdg-open` na pasta pai como fallback — declarado, não entregue |

Atrás de uma porta, com o adaptador Windows como única implementação, pela razão do RFC-027 §4.1: um adaptador não testado é uma afirmação de portabilidade que ninguém verificou. §5.1 é o argumento a favor dessa regra em miniatura: a única linha de comando que este RFC escreveu antes de rodar estava errada.

## 6. Os dois guardas

§2.1 mostrou que o argumento do RFC-026 dissolve **porque a implantação é local**. Uma condição que sustenta uma decisão de segurança precisa ser verificada em execução, não presumida na leitura.

**Guarda 1 — configuração.** `settings.allow_local_file_actions`, default **`False`**. Segue exatamente o precedente de `warm_up_models` (RFC-026 §10): *"o default seguro é o que não pode baixar 600 MB para dentro de um processo que não pediu"*. Aqui, o default seguro é o que não pode abrir janelas numa máquina que não pediu. Desligado, `/reveal` responde 404 — a rota não existe, em vez de existir e recusar.

**Guarda 2 — loopback.** Independente da configuração, `/reveal` recusa requisições cujo `request.client.host` não seja de loopback. Isso vale mesmo com o guarda 1 ligado, e mesmo se alguém subir a API em `0.0.0.0` sem perceber — o que é precisamente o cenário em que o argumento original do RFC-026 volta a valer.

Os dois são independentes de propósito. O primeiro é intenção do operador; o segundo é um fato sobre quem está chamando. Nenhum dos dois substitui o outro.

**Implementação** (`presentation/local_file_actions.py`). Os dois são dependências do FastAPI declaradas no decorador da rota, que o FastAPI resolve antes dos parâmetros do próprio handler: uma requisição recusada nunca constrói a use case, nunca abre sessão de banco e nunca enumera volumes (um teste conta as construções). A ordem é guarda 1 e depois guarda 2, de modo que um chamador remoto com a configuração desligada recebe o mesmo 404 de uma rota inexistente — corpo idêntico, fixado por teste — e não um 403 que admitiria que a rota existe. O que isso garante é o *comportamento*, não sigilo: o OpenAPI em `/docs` continua listando a rota, e um `GET` nela recebe 405 como em qualquer rota só-`POST` — a forma da API é pública de qualquer jeito, e o que o guarda 1 impede é que ela aja. Com a configuração ligada, o guarda 2 responde **403**. Só endereços IP literais de loopback contam: `127.0.0.0/8`, `::1` e o IPv4 mapeado em IPv6 (`::ffff:127.0.0.1`, que um socket dual-stack reporta); `localhost` é um nome, e um nome ali significa que algo antes reescreveu o endereço. Uma requisição sem endereço de cliente é recusada: desconhecido não é local.

Uma armadilha de teste que inverteu a dificuldade esperada: o `TestClient` do Starlette reporta o par como `("testclient", 50000)`, que nem é um endereço. O teste "não-loopback é recusado" passa sem esforço nenhum, e um teste do caminho permitido que esqueça `client=("127.0.0.1", …)` é barrado pelo guarda 2 e nunca exercita a rota. `test_reveal_guards.py` escreve o caminho permitido primeiro e declara o endereço de todo cliente.

Medido pelo `TestClient`, em processo (`measure_reveal_and_revalidation.log`, parte 1): guarda 1 desligado responde em **1,66 ms** de mediana; guarda 2 recusando um chamador da rede, **2,09 ms**; o 204 permitido, com resolução de localização e checagem do arquivo, **3,74 ms**; 0 processos lançados em 1.050 requisições recusadas de cada tipo. Criar o processo em si — medido com `cmd.exe /c exit 0` como substituto, na mesma forma de argumentos, porque lançar o Explorer abriria janelas na máquina de quem roda o script — custa **2,36 ms** de mediana. Nada disso justifica cache nem atalho.

O caminho na resposta de busca (§4) **não** fica atrás do guarda 1 — ele é o coração do produto e o guarda 2 já cobre o cenário de exposição. Um sistema que exige configuração para cumprir sua função principal está com o default errado.

> **Observação, não corrigida.** A frase acima diz que o guarda 2 "cobre o
> cenário de exposição" do caminho, mas §6 define o guarda 2 **só para
> `/reveal`**, e foi assim que ele foi implementado. Com a API em `0.0.0.0`, um
> chamador da rede local recebe `relative_path`, `absolute_path` e as thumbnails
> de qualquer busca — exatamente o vazamento que o RFC-026 descrevia. Aplicar o
> guarda de loopback também aos caminhos e às thumbnails é uma decisão de
> produto (bloquear, omitir os caminhos, ou aceitar), e o `relative_path` de um
> disco interno já contém o nome do usuário do Windows, o que tira o sentido de
> omitir só o absoluto. Ficou registrada em §9 em vez de decidida aqui por
> dedução.

## 7. Thumbnails

O `ARCHITECTURE.md` já decidiu a forma, e a decisão continua certa:

> *"Thumbnails são geradas uma vez durante a indexação e armazenadas em disco. São servidas ao frontend como arquivos estáticos por um endpoint dedicado, nunca embutidas como base64 nas respostas da API."*

Este RFC acrescenta o que aquele parágrafo não cobria.

### 7.1 Onde elas ficam — e onde não ficam

Em um diretório gerenciado pelo app (`settings.thumbnail_directory`), endereçadas por `images.id`. **Nunca no acervo.** O RFC-028 §10 já declarou a regra: *"o sistema nunca escreve no acervo"*. Um usuário cujo HD de fotos ganha uma pasta `.solidvision/` que ele não criou perde a confiança que a premissa local-first depende.

A consequência é a propriedade de §4.1: as thumbnails vivem em um disco que está **sempre** conectado, então continuam servíveis quando o disco de origem está na gaveta. Guardá-las junto das fotos teria destruído exatamente o caso de uso que mais precisa delas.

**Implementação.** O default é `%LOCALAPPDATA%\SolidVision\thumbnails` — e não um caminho relativo ao diretório corrente como `log_directory`, que ficaria ao lado do `data/images` de `indexing_root_path` e dentro do acervo no instante em que alguém indexasse `data/`. `LOCALAPPDATA` e não `APPDATA`, porque é cache reconstruível e o Windows faz *roaming* de `APPDATA` em domínio. Um JPEG por id, em subpastas pelos dois primeiros dígitos hexadecimais do id (256 pastas de algumas centenas de arquivos em vez de uma pasta de 100.000). `images.thumbnail_path` guarda a localização **relativa ao cache**, pelo motivo de `relative_path` ser relativo ao dispositivo: mover o cache não pode deixar toda linha apontando para o nada. A escrita é atômica — nome temporário e `os.replace` — porque o mesmo id é sobrescrito quando a foto muda e uma requisição servida no meio de uma escrita simples entregaria meio JPEG.

**Um risco que esta seção não via: a varredura encontrar o próprio cache.** Indexar o disco do sistema inteiro — um dispositivo legítimo desde o RFC-027 — passaria por `%LOCALAPPDATA%`, descobriria cada thumbnail como uma foto nova, geraria a thumbnail *dela*, e a encontraria na varredura seguinte: um acervo que cresce a cada vez que é varrido. `FilesystemImageProvider` ganhou `excluded_directories`, e toda composição que varre — o executor de jobs, `capture_date_backfill`, `thumbnail_backfill` — passa o cache. Um teste roda o executor duas vezes com o cache dentro do disco e exige seis imagens nas duas, não doze.

Um teste varre o disco de teste antes e depois de um job real (executor, gerador Pillow e armazenamento em disco de verdade) e compara nome, tamanho e data de modificação de cada arquivo: **nenhuma escrita sob a raiz do dispositivo.**

### 7.2 Quando são geradas — de graça, quase

O pipeline do RFC-024 já abre e decodifica cada imagem com Pillow para alimentar o CLIP. Uma thumbnail de 512 px sai dessa imagem **já decodificada em memória**; o custo é redimensionar e codificar, sem nenhuma leitura de disco a mais.

> **Correção (§7.2).** Não sai. A imagem decodificada é uma variável local de
> `ClipEmbeddingModel._preprocess()`, dentro do adaptador que vive atrás de
> `EmbeddingModelPort`; o método devolve um tensor de tamanho fixo e a imagem
> fica inalcançável no instante em que ele retorna — é essa, aliás, a
> estratégia de memória dele. `IndexOrUpdateImagesUseCase`, onde a thumbnail é
> gerada, nunca vê a imagem. E nenhuma das outras duas aberturas do arquivo
> durante a indexação produz algo reaproveitável: `read_capture_date()` lê só o
> cabeçalho EXIF, de propósito (RFC-028 §6), e `Sha256ContentHasher` lê bytes
> em streaming sem decodificar.
>
> **A thumbnail é uma quarta abertura independente**, consistente com o padrão
> que o projeto já tinha — cada leitura do arquivo para um fim estreito — e não
> uma exceção a ele. `ThumbnailGeneratorPort` (Domain, ao lado de
> `ContentHasherPort`) e `PillowThumbnailGenerator` (Infrastructure).
>
> **Alternativa recusada:** fazer `EmbeddingModelPort.encode_images` devolver
> também a imagem decodificada, ou os bytes da thumbnail. Daria ao adaptador de
> embeddings uma responsabilidade que não tem nada a ver com embeddings, e
> obrigaria todo duplo do modelo a saber desenhar thumbnails.
>
> **O custo, portanto, é uma decodificação completa mais redimensionamento e
> codificação**, e foi medido contra a inferência ao lado dele, não contra
> zero (`measure_thumbnail_cost.log`):
>
> | corpus | thumbnail sozinha (mediana) | tamanho médio |
> | --- | --- | --- |
> | demo, 45 fotos reais de ~1024 px | **21,3 ms** | **45,2 KB** |
> | sintético 12 MP (4000×3000) | **96,2 ms** | 55,9 KB |
> | sintético 20 MP (5472×3648) | **109,4 ms** | 49,8 KB |
>
> Os corpora grandes são fotos do demo ampliadas com LANCZOS e salvas a
> qualidade 90: dimensões e tamanhos de arquivo realistas, conteúdo mais liso do
> que um sensor real produz — arquivos reais comprimem pior e podem decodificar
> um pouco mais devagar. Todos os arquivos estavam no cache do sistema
> operacional: é custo de decodificação, não de busca em disco mecânico.
>
> **Contra a inferência, pelo pipeline inteiro com o modelo real** (lote 8,
> SHA-256, repositório em memória, execuções sem/com/sem thumbnails para expor
> a deriva):
>
> | corpus | sem thumbnails | com thumbnails | thumbnail por imagem | inferência por imagem, mesma execução |
> | --- | --- | --- | --- | --- |
> | demo, 45 fotos | 2,28 e 2,21 img/s | **2,09 img/s** | 26,9 ms | 450 ms (**6,0%**) |
> | sintético 20 MP, 20 fotos | 1,05 e 0,95 img/s | **0,88 img/s** | 114,3 ms | 1.003 ms (**11,4%**) |
>
> No demo, a thumbnail somou 1,50 s (+7,5%) ao tempo de parede frente à média
> das duas execuções sem. No corpus de 20 MP a diferença entre as **duas
> execuções sem thumbnail** já foi de 11% — do mesmo tamanho do efeito —, então
> o número confiável ali é o que o próprio `IndexingSummary` cronometra
> (`thumbnail_seconds`, separado de `inference_seconds` exatamente para isso),
> não a diferença de relógio.
>
> Projetado para 100.000 imagens: **0,6 h** de renderização a 21 ms, até
> **3,0 h** a 109 ms, contra 12,5 h de inferência a 450 ms. Longe de "de graça";
> bem abaixo da inferência, que era a expectativa, e agora um número.
>
> **A ordem dos passos é a otimização, e também foi medida.** `thumbnail()`
> vem antes de qualquer coisa que force a decodificação completa, porque para
> JPEG ele pede ao decodificador um rascunho em escala reduzida (DCT a 1/2, 1/4
> ou 1/8) antes de redimensionar. Aplicar a orientação EXIF ou converter o modo
> antes custaria **167,8 ms** em vez de 96,2 ms a 12 MP e **254,4 ms** em vez de
> 109,4 ms a 20 MP. A orientação é aplicada depois, na imagem pequena — a tag
> sobrevive ao `thumbnail()` — e a thumbnail sai sem EXIF, para nenhum
> visualizador girá-la de novo.
>
> Três casos difíceis do RFC-022 §6.2 mudaram o adaptador em relação a
> `convert("RGB")` puro, cada um por algo que o usuário veria: TIFF de 16 bits
> é escalado para 8 bits (a conversão direta do Pillow corta todo valor acima
> de 255 e renderiza a foto quase toda branca); transparência é composta sobre
> branco (JPEG não tem alfa, e a conversão direta põe as áreas transparentes
> sobre preto); e a orientação EXIF é aplicada (uma foto tirada na vertical
> apareceria deitada).

Custo por imagem, e o quanto ele desloca as 2,2 imagens/s do RFC-024: ~~`TBM`~~ **21–27 ms por imagem no corpus de demonstração (6,0% do tempo de inferência; 2,2 → 2,09 img/s) e ~110 ms a 20 MP (11,4%)** — ver a correção acima. A expectativa é que fique bem abaixo dos ~450 ms/imagem da inferência, mas o RFC-024 §17 tem uma seção inteira sobre hipóteses que os números mataram, e esta é uma hipótese. *Esta sobreviveu, pela razão errada: não por reaproveitar a decodificação, que não acontece, mas porque decodificar em escala reduzida é barato.*

**Quando, exatamente.** Para cada imagem que chegou a `EMBED` **e sobreviveu à inferência** — inclusive na re-tentativa imagem a imagem depois de um lote que falhou —, depois do lote de inferência e antes da escrita, para que a linha seja gravada uma vez com a localização dentro. Nunca para `SKIP_UNCHANGED` nem `REFRESH_METADATA`: um arquivo inalterado já tem a thumbnail que seus bytes merecem, ou não tem e é do backfill; renderizar ali transformaria um re-scan, que custa um `stat` por arquivo, numa decodificação por arquivo. **Uma falha ao renderizar nunca é falha da imagem**: o embedding foi pago, a imagem é persistida sem thumbnail e continua buscável, e a falha vai para `summary.thumbnail_failures` — nunca para `failures`, nunca para o contador de falhas do job.

Imagens indexadas antes deste RFC não têm thumbnail. O backfill é uma varredura sem modelo, como o do RFC-028 §7, e exige o dispositivo conectado.

**Implementação:** `python -m app.infrastructure.workers.thumbnail_backfill --root PATH`, composto linha a linha como `capture_date_backfill` — mesmas janelas de pré-leitura, uma escrita em lote por janela com recuo por linha, `--dry-run`, e `--force` para renderizar de novo linhas que já têm thumbnail (depois de mudar `THUMBNAIL_MAX_EDGE`, por exemplo; uma linha cujo arquivo falha ao renderizar sob `--force` mantém a thumbnail que tinha). Saber quais linhas já têm thumbnail vem de graça da pré-leitura: `IndexMetadata` ganhou `thumbnail_path`, pelo precedente exato de `capture_source` — **não é sinal de mudança, e `plan_indexing()` nunca o lê**. É a localização e não um booleano porque a rota de §7.2 também precisa dela; um booleano teria deixado a coluna sem leitor. `test_thumbnail_backfill.py` verifica no grafo real de imports, num interpretador novo, que o comando não carrega `torch` nem o adaptador CLIP.

`GET /api/v1/images/{id}/thumbnail` serve os bytes. 404 quando ainda não foi gerada; a UI mostra um placeholder e não um erro.

**Implementação.** Uma rota controlada, não um *mount* `StaticFiles`, que não saberia responder 404 para um id sem thumbnail nem conferir `If-None-Match` contra o hash do conteúdo antes de tocar no disco. Três situações dão 404, todas com placeholder: o id não existe; a linha não tem thumbnail; ou a linha aponta para um arquivo que o cache não tem mais. Esta última **não** é 410: 410 é para o arquivo do usuário, que o `/reveal` reporta; o cache é do próprio app, e a ausência dele é uma limpeza ou um bug deste lado, não o mundo mudando sob o pedido. A thumbnail é servida **com o disco de origem desconectado** — a use case nem recebe um `DeviceLocator` —, e há teste disso.

**Correção em relação a uma versão anterior deste RFC.** Um rascunho anterior justificava cache agressivo (`Cache-Control: immutable`) alegando que *"mudar a foto muda o caminho e portanto o id"*. Isso está errado, e vale registrar o erro em vez de apagá-lo: sob o RFC-027, `id = uuid5(ns, f"{device_id}/{relative_path}")` — depende só do **caminho**, nunca do conteúdo. Um usuário que sobrescreve a mesma foto no mesmo lugar (reexportar, editar e salvar por cima) produz o **mesmo id**, e uma resposta marcada `immutable` faria o navegador continuar servindo a thumbnail antiga da própria memória, sem nunca revalidar.

O que de fato acontece quando o conteúdo muda: a decisão incremental do RFC-020 compara `file_size`/`file_modified_at`/`content_hash` e, se algum diverge, marca o arquivo como candidato a reprocessamento — o mesmo evento que já dispara um novo embedding (RFC-024). §7.2 gera a thumbnail **nesse mesmo reprocessamento**, a partir da imagem já decodificada, e escreve por cima do arquivo endereçado pelo id de sempre. O mecanismo funciona; a frase anterior só descrevia o motivo errado.

Isso torna `Cache-Control: immutable` inválido — e a correção certa não é trocar por um TTL curto, que jogaria fora o benefício de cache sem necessidade, mas usar validação condicional: `ETag` calculado a partir de `images.content_hash`, que o RFC-024 já computa e armazena para a decisão incremental (§6.1 do RFC-028 já reaproveita esse mesmo campo pelo mesmo motivo — não pagar duas vezes por um dado que já existe). Com `Cache-Control: max-age=31536000` **e** `ETag: "<content_hash>"`, um cliente que já tem a thumbnail em cache manda `If-None-Match` e recebe `304` quando o conteúdo não mudou — sem baixar bytes de novo — e recebe a thumbnail nova, servida normalmente, no instante em que `content_hash` diverge. O ganho de desempenho do cache agressivo é preservado; a correção que faltava é a revalidação, não a ausência de cache.

> **Correção (§7.2, de novo).** A correção acima reintroduz o erro que
> corrige. Uma resposta com `max-age=31536000` fica **fresca por um ano**, e uma
> resposta fresca é servida do cache do navegador **sem consultar o servidor**
> (RFC 9111 §4.2). O navegador só manda `If-None-Match` quando a cópia já está
> velha — e com `max-age` de um ano, isso é daqui a um ano. A foto sobrescrita
> no mesmo caminho continuaria mostrando a thumbnail antiga pelo mesmo tempo que
> com `immutable`; a única diferença é que um recarregamento forçado a
> revalidaria.
>
> A diretiva que diz o que a frase quer — *guarde, mas pergunte antes de usar* —
> é **`no-cache`** (RFC 9111 §5.2.2.4). A implementação envia
> `ETag: "<content_hash>"` com `Cache-Control: private, no-cache`: o navegador
> guarda a thumbnail e revalida a cada uso, e uma cópia atual custa um `304`
> sem corpo. `private` impede que um cache compartilhado entre o usuário e a
> própria máquina, se algum dia existir, guarde as fotos dele. Linhas indexadas
> antes do RFC-024 não têm `content_hash`, e são servidas com `no-store`: um
> validador que nunca casa é um cache que falha em silêncio.
>
> **O preço foi medido, e é o preço declarado de nunca mostrar uma foto
> velha** (`measure_reveal_and_revalidation.log`, parte 4, pela composição de
> produção sobre PostgreSQL, em processo): um `304` custa **6,6 ms** de mediana
> e o `200` com os 45 KB da thumbnail, **8,6 ms**. A maior parte é a leitura de
> metadados, então numa API local a revalidação economiza bytes mais do que
> tempo, e uma página de 50 thumbnails a paga 50 vezes (~330 ms em sequência;
> um navegador paraleliza). Um `max-age` curto trocaria parte disso por uma
> janela limitada de thumbnail velha; não foi adotado, e fica em §9.
>
> **O `304` não lê a thumbnail do disco** — verificado por contagem, não pelo
> tempo: em 550 revalidações com o hash atual, **0** chamadas a
> `FilesystemThumbnailStore.locate()` e **0** `FileResponse` construídos; em 550
> requisições com hash velho, 550 e 550. A decisão é tomada na use case com uma
> leitura de metadados, antes de o armazenamento ser consultado. Uma
> consequência aceita: um cliente com o hash atual recebe `304` mesmo se o
> arquivo do cache tiver sido apagado depois — a cópia dele continua sendo a
> foto certa.
>
> Um detalhe de implementação que o prompt de aplicação previa ao contrário: o
> `FileResponse` do Starlette 1.3.1 **gera, sim**, um `ETag` sozinho, a partir
> de `mtime` e tamanho do arquivo, com `setdefault` — o `ETag` explícito
> prevalece. Mas ele não trata `If-None-Match`; a comparação é feita à mão, com
> comparação fraca (`W/"…"` casa com `"…"`), listas de tags e aspas opcionais,
> como a RFC 9110 §13.1.2 pede para esse cabeçalho.

O teste que fixa a correção desta seção passa pelo pipeline real: indexa uma foto, pede a thumbnail, sobrescreve a foto no mesmo caminho com outro conteúdo, reindexa, e confere que o id é o mesmo, que o `ETag` mudou, que a revalidação com o `ETag` antigo recebe `200` com bytes diferentes, e que o `ETag` novo recebe `304`.

### 7.3 A janela do RFC-027 fecha aqui

O RFC-027 §6.2 justificou sua prioridade pela ausência de referências a `images.id`, e o RFC-029 §11 verificou que jobs não criam nenhuma.

Este RFC cria: `thumbnail_path` é derivado de `images.id`, e os arquivos em disco são nomeados por ele. Reescrever a PK a partir daqui significa renomear arquivos junto, e o passo deixa de ser um `UPDATE` em uma tabela.

**Isto é registrado como consequência assumida, não descoberta depois.** É a última confirmação de que a ordenação dos quatro RFCs estava certa. A migration `f4b9e2d7c615` não cria chave estrangeira, índice nem restrição — a referência a `images.id` está nos nomes dos arquivos, não no esquema —, e um teste lê isso do código da migration.

## 8. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Manter o caminho fora da resposta | O produto não entrega recuperação sem ele (§1) |
| Caminho absoluto armazenado no banco | O RFC-027 o removeu porque muda sozinho; guardá-lo de novo desfaz aquilo (§2.2) |
| Caminho como identificador em rotas | Instável, e reabre travessia de diretório; o id já é estável (§2.2, §5.1) |
| `/reveal` aceitando o caminho no corpo | Cria uma superfície de injeção que o desenho por id torna inexistente (§5.1) |
| `subprocess` com `shell=True` | Um shell para interpretar metacaracteres onde nenhum é necessário (§5.1) |
| `GET /reveal` | Pré-busca abriria janelas sem clique (§5.2) |
| Só o guarda de loopback | Não expressa intenção do operador; um túnel local o satisfaz (§6) |
| Só o guarda de configuração | Não protege quem subiu em `0.0.0.0` sem perceber (§6) |
| Ações locais ligadas por default | O precedente de `warm_up_models` é o default que não age sem ser pedido (§6) |
| Caminho na busca atrás de configuração | Exigiria configurar o sistema para ele cumprir a função principal (§6) |
| Thumbnails no acervo | O sistema nunca escreve no acervo, e o disco offline deixaria de ter thumbnail (§7.1) |
| Thumbnails em base64 na resposta | Já rejeitado pelo `ARCHITECTURE.md`; infla o JSON e impede cache do navegador (§7) |
| Thumbnails sob demanda na primeira leitura | Exige o disco conectado exatamente quando ele costuma não estar (§7.1) |
| Servir a imagem original completa | Uma foto de drone tem dezenas de MB; a thumbnail resolve a visualização (§10) |
| `Cache-Control: immutable` sem `ETag` | Rascunho anterior deste RFC. Presumia que o id muda quando o conteúdo muda — não muda, é derivado do caminho (RFC-027). Serviria bytes obsoletos para sempre após uma foto ser sobrescrita no mesmo caminho (§7.2) |
| `Cache-Control: max-age=31536000` com `ETag` | Versão proposta deste RFC. Uma resposta fresca é servida sem revalidar, então o `ETag` nunca seria enviado de volta durante um ano — o mesmo defeito de `immutable` (§7.2, corrigido) |
| Renomear a thumbnail a cada reprocessamento (versionar por hash no nome do arquivo) | Resolveria a invalidação, mas exige limpar o arquivo antigo e complica o endereçamento por id que o resto do RFC padroniza; `ETag` com `content_hash` já existente resolve sem isso (§7.2) |
| Reaproveitar a imagem decodificada pelo CLIP | Não está alcançável: é local ao adaptador. Expô-la pela porta de embeddings daria ao adaptador uma responsabilidade alheia e obrigaria todo duplo do modelo a desenhar thumbnails (§7.2, corrigido) |
| Renderizar thumbnail também para arquivos pulados pela decisão incremental | Transformaria o re-scan de um `stat` por arquivo numa decodificação por arquivo; o backfill cobre as linhas antigas uma vez (§7.2) |
| `subprocess.run` para o Explorer | Espera o processo; com "janelas de pasta em processo separado" o processo é a janela, e a requisição ficaria pendurada até o usuário fechá-la (§5.1, corrigido) |
| `/select,<caminho>` como um item só | Abre a pasta Documentos quando o caminho tem espaço — verificado (§5.1, corrigido) |

## 9. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **`/reveal` executa um processo na máquina do usuário** | Dois guardas independentes, entrada só por UUID, sem shell, `explorer.exe` por caminho absoluto (§5.1, §6) |
| **Com a API em `0.0.0.0`, caminhos e thumbnails ficam visíveis na rede local** | **Aberto.** O guarda 2 protege só `/reveal`; a frase de §6 que diz que ele "cobre o cenário de exposição" não se sustenta contra a definição do próprio §6. Decidir entre recusar, omitir ou aceitar é de produto (§6, observação) |
| **A janela de reescrita barata da PK fecha aqui** (§7.3) | Consequência assumida; a ordem dos RFCs foi escolhida por isso |
| **Só Windows tem adaptador de revelação** (§5.3) | Deliberado; a porta existe |
| **Cache de thumbnails cresce sem política de limpeza** | Trabalho futuro. **45,2 KB** por imagem no corpus de demonstração e ~53 KB nos sintéticos grandes: **4,3 a 5,0 GB por 100.000 imagens**, em `%LOCALAPPDATA%`. Não é urgente para um acervo pessoal, mas é o maior dado que o app escreve, e uma imagem apagada do acervo deixa a thumbnail para trás |
| **Uma thumbnail pode ficar obsoleta se o cache do cliente não revalidar** | Mitigado por `ETag` sobre `content_hash` com `no-cache`, não por imutabilidade do id nem por `max-age` — o id **não** muda quando o conteúdo muda, e uma resposta fresca não é revalidada (§7.2, corrigido duas vezes) |
| **Revalidar custa uma leitura de metadados por thumbnail** | 6,6 ms por `304` em processo; uma página de 50 thumbnails a paga 50 vezes. Um `max-age` curto reduziria isso ao preço de uma janela limitada de foto velha — não adotado sem medir a UI real (§7.2) |
| **Backfill de thumbnails exige o dispositivo conectado** (§7.2) | Consequência do RFC-027 |
| Um caminho com caracteres fora do ASCII | `subprocess` com lista de argumentos trata; teste dedicado — e a verificação real de §5.1 usou `fazenda São João.jpg` numa pasta `com espaço e ação` |
| **A varredura encontrar o próprio cache de thumbnails** | Não previsto por este RFC; resolvido com `excluded_directories` em toda composição que varre (§7.1) |
| **Mudar `THUMBNAIL_MAX_EDGE` não muda o `ETag`** | Aceito. O `ETag` é o hash da *foto*; um cliente que já tem a thumbnail no tamanho antigo a mantém até a foto mudar — uma imagem menor da foto certa, não uma imagem errada. `thumbnail_backfill --force` regenera o cache |
| **Uma thumbnail gravada antes de a linha falhar ao persistir** | Aceito: o arquivo fica órfão até a próxima execução para aquela imagem sobrescrevê-lo |

Trabalho futuro: política de retenção do cache; adaptadores macOS/Linux; servir a imagem original em resolução intermediária para um visualizador; e "abrir com o aplicativo padrão", que é a mesma superfície de segurança de `/reveal` e cabe atrás do mesmo guarda.

## 10. Não-objetivos

- Servir a imagem original completa (§8)
- Visualizador, lightbox, zoom, comparação lado a lado
- Escrever qualquer coisa no acervo — inclusive EXIF, renomeação, mover, apagar (§7.1)
- Copiar, exportar ou compartilhar arquivos a partir da UI
- Montar dispositivos automaticamente
- Autenticação e múltiplos usuários — os guardas de §6 são de implantação, não de identidade
- Adaptadores macOS e Linux (§5.3)
- Retenção/limpeza do cache de thumbnails (§9)
- A UI em si

## 11. Entregáveis

> **Correção (§11).** A tabela proposta dizia:
>
> > *"`backend/app/infrastructure/filesystem/file_revealer.py` — Porta + adaptador Windows (§5.3)"*
>
> A porta não pode morar em Infrastructure: `RevealImageUseCase` depende dela,
> e a Application não importa Infrastructure (`test_application_architecture.py`
> falha se importar). `FileRevealerPort` foi para
> `domain/services/file_revealer_port.py`, ao lado das outras portas; o arquivo
> de Infrastructure tem só o adaptador.
>
> E dizia que o `ARCHITECTURE.md` *"§15 `Images` ganha `thumbnail_path`"*. **Já
> tinha** — a tabela `Images` e a subseção "Thumbnail Serving" existiam antes
> deste RFC. O que faltava era onde as thumbnails não ficam, quando são geradas
> e a estratégia de cache, e foi isso que a subseção ganhou, em vez de ser
> duplicada.

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/exceptions/file_access_errors.py` | `FileGoneError` (410) e `ThumbnailNotFoundError` (404); o 409 reaproveita `DeviceNotConnectedError` |
| `backend/app/domain/services/thumbnail_generator_port.py` | Renderizar uma thumbnail com uma decodificação própria (§7.2) |
| `backend/app/domain/services/thumbnail_store_port.py` | Guardar e localizar thumbnails num armazenamento do app (§7.1) |
| `backend/app/domain/services/file_revealer_port.py` | A porta de §5.3 (correção de §11) |
| `backend/app/application/use_cases/resolve_image_location.py` | `device.connected` e `absolute_path` por requisição, uma enumeração por disco (§4) |
| `backend/app/application/use_cases/thumbnail_writer.py` | Gerador + armazenamento + tamanho, compartilhado pelo pipeline e pelo backfill |
| `backend/app/application/use_cases/get_image_details.py` | §4.2 |
| `backend/app/application/use_cases/get_thumbnail.py` | §7.2, com a decisão do `304` antes de consultar o armazenamento |
| `backend/app/application/use_cases/reveal_image.py` | §5 |
| `backend/app/application/use_cases/backfill_thumbnails.py` | §7.2 |
| `backend/app/infrastructure/filesystem/file_revealer.py` | `WindowsFileRevealer` (§5.1, §5.3) |
| `backend/app/infrastructure/filesystem/thumbnail_generator.py` | `PillowThumbnailGenerator` (§7.2) |
| `backend/app/infrastructure/filesystem/thumbnail_store.py` | `FilesystemThumbnailStore`: escrita atômica, localização relativa, recusa de localização fora do cache (§7.1) |
| `backend/app/infrastructure/workers/thumbnail_backfill.py` | §7.2 |
| `backend/app/presentation/local_file_actions.py` | Os dois guardas (§6) |
| `backend/app/presentation/schemas/image_schema.py` | O placeholder do RFC-026, finalmente preenchido |
| `backend/alembic/versions/f4b9e2d7c615_add_image_thumbnail_path.py` | `images.thumbnail_path` |
| `backend/tests/presentation/test_image_details_api.py` | §4.2 e §7.2, inclusive o `ETag` que muda com a foto e o `304` sem leitura de arquivo |
| `backend/tests/presentation/test_reveal_guards.py` | Os dois guardas, o 404 com o guarda 1 desligado, a assinatura sem caminho, 409 e 410 (§5, §6) |
| `backend/tests/infrastructure/filesystem/test_thumbnail_generator.py` | Os casos difíceis do RFC-022 §6.2 |
| `backend/tests/infrastructure/filesystem/test_thumbnail_store.py` | §7.1 |
| `backend/tests/infrastructure/filesystem/test_file_revealer.py` | A linha de comando exata, sem shell (§5.1) |
| `backend/tests/infrastructure/persistence/test_thumbnail_path_contract.py` | Três implementações de repositório, um contrato |
| `backend/tests/infrastructure/workers/test_thumbnail_backfill.py` | Grafo de imports sem modelo, e o comando de ponta a ponta |
| `backend/tests/infrastructure/workers/test_thumbnails_stay_out_of_the_collection.py` | Nenhuma escrita no acervo; o cache dentro do disco não é indexado (§7.1) |
| `backend/tests/application/test_resolve_image_location.py` | Uma enumeração por disco (§4) |
| `backend/tests/application/test_file_access_use_cases.py` | As três use cases de leitura |
| `backend/tests/application/test_backfill_thumbnails.py` | §7.2 |
| `experiments/rfc-030-file-access/measure_thumbnail_cost.py` | A medição de §7.2 e §9 |
| `experiments/rfc-030-file-access/measure_reveal_and_revalidation.py` | §6, §7.2 e §12 |
| `experiments/rfc-030-file-access/verify_explorer_select.ps1` | A verificação de §5.1 — abre e fecha janelas do Explorer, por isso é script e não teste |
| `docs/rfcs/rfc-030-acesso-ao-arquivo.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/presentation/schemas/search_schema.py` | `device`, `relative_path`, `absolute_path` (`captured_at` já estava, desde o RFC-028); docstring reescrita com o resultado de §2, citando a frase antiga |
| `backend/app/presentation/api/v1/routers/images.py` | As três rotas novas; `/thumbnail` responde `ETag`/`If-None-Match` sobre `content_hash` (§7.2); a busca resolve localizações depois de buscar e registra `locate_ms` à parte de `elapsed_ms` |
| `backend/app/presentation/dependencies/__init__.py` | Provedores das use cases, do armazenamento de thumbnails e do revelador |
| `backend/app/presentation/error_handlers.py` | `GoneError` → 410 (§5.1) |
| `backend/app/domain/exceptions/domain_error.py`, `image_errors.py`, `__init__.py` | `GoneError`; `ImageNotFoundError` re-baseada (§4.2) |
| `backend/app/domain/value_objects/index_metadata.py`, `indexing_record.py` | `thumbnail_path` (§7.2) |
| `backend/app/domain/repositories/image_repository.py` | `update_thumbnail_path[_many]`; contrato do `thumbnail_path` em `save_indexed` e na pré-leitura |
| `backend/app/infrastructure/database/models/image_model.py` | `thumbnail_path` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py`, `in_memory_image_repository.py` | Leem e escrevem `thumbnail_path`; `update_index_metadata` o deixa intacto |
| `backend/app/application/use_cases/index_or_update_images.py` | Gera a thumbnail para o que sobreviveu à inferência, isolada por arquivo; `thumbnails_written`, `thumbnail_seconds`, `thumbnail_failures` (§7.2) |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | `excluded_directories` (§7.1) |
| `backend/app/infrastructure/workers/job_runner.py` | `build_runner()` compõe o `ThumbnailWriter` e exclui o cache da varredura |
| `backend/app/infrastructure/workers/capture_date_backfill.py` | Exclui o cache da varredura |
| `backend/app/infrastructure/config/settings.py` | `allow_local_file_actions`, `thumbnail_directory`, `thumbnail_max_edge` |
| `backend/tests/presentation/test_search_route.py` | `test_no_result_exposes_a_server_path`, que fixava a decisão do RFC-026, virou o teste da decisão contrária, com a história na docstring |
| `backend/tests/presentation/test_error_handlers.py` | 404/409/410 juntos; expectativas escritas em vez de derivadas da hierarquia (§4.2) |
| `backend/tests/presentation/test_search_api_integration.py` | O resultado num disco não plugado, pelo resolvedor de produção e a enumeração real de volumes (nível 2) |
| `backend/tests/presentation/test_search_api_e2e.py`, `backend/tests/dataset/test_semantic_search_e2e.py` | O nível 3 passa a entregar ao repositório de dispositivos a mesma transação do corpus: a busca agora lê o disco de cada resultado, e a linha do dispositivo só existe dentro dela |
| `backend/tests/application/fakes.py` | Duplos das três portas novas |
| `.env.example` | As três chaves novas |
| `ARCHITECTURE.md` | §15 "Thumbnail Serving" ganha onde não ficam, quando são geradas e a estratégia de cache; nova subseção "Opening a File"; a tabela `Images` descreve `thumbnail_path` (correção de §11) |
| `AI_Context.md` | Thumbnails como arquivos no cache do app; o comando de backfill; nenhuma rota aceita caminho |

## 12. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | **1429 passed**, 58 deselected (linha de base antes deste RFC: 1244 passed e 1 erro de *deadlock* pré-existente na fixture, que passa isolado — `baseline.log`) |
| `pytest -m slow` | **58 passed** |
| `black --check .` / `ruff check .` | limpos |
| `mypy` | **0 erros** em 216 arquivos |
| `alembic heads` | `f4b9e2d7c615`, head único |
| `alembic downgrade`/`upgrade` | exercitados de verdade contra o PostgreSQL: a coluna existe em `f4b9e2d7c615`, some em `e7a2c9b41f30`, volta em `f4b9e2d7c615` |
| `/reveal` com `allow_local_file_actions=false` | 404 com corpo idêntico ao de uma rota inexistente, mesmo de loopback e mesmo de um chamador remoto; nenhum processo lançado; a use case nunca é construída |
| `/reveal` de cliente não-loopback | 403 com a configuração ligada, inclusive o `TestClient` padrão; nenhum processo lançado |
| `/reveal` de loopback com a configuração ligada | 204, lançando `explorer.exe /select, <caminho montado pelo servidor>` sem shell; aceita `127.x`, `::1` e `::ffff:127.0.0.1` |
| `/reveal` nunca aceita string de caminho | Teste de assinatura: handler, *dependant* e OpenAPI sem parâmetro além do id; caminho no corpo e na query ignorados |
| `/reveal` com disco desconectado | 409 via `DeviceNotConnectedError` reaproveitado, com o rótulo do disco na mensagem |
| `/reveal` com arquivo sumido e disco conectado | 410 via `FileGoneError` |
| `/select,` com caminho com espaço e acento | Seleciona o arquivo, verificado no Explorer real pelo adaptador real (§5.1) |
| Nenhuma escrita sob a raiz do dispositivo durante indexação | Sim — nome, tamanho e `mtime` de cada arquivo do disco de teste iguais antes e depois de um job real com thumbnails |
| Cache de thumbnails dentro do disco indexado | Não é indexado: duas execuções, seis imagens nas duas |
| Falha ao gerar thumbnail | Não aparece em `failures` nem no contador do job; a imagem é indexada com `thumbnail_path` nulo |
| Imagem cujo embedding falhou | Não tem thumbnail renderizada |
| Thumbnail servida com o dispositivo desconectado | Sim — fixa a premissa de §4.1 |
| Reprocessar uma imagem (conteúdo mudou, id igual) muda o `ETag` da thumbnail | Sim, pelo pipeline real: mesmo id, `ETag` novo, bytes novos para o `ETag` antigo, `304` para o novo |
| Requisição com `If-None-Match` do hash correto recebe `304` sem corpo | Sim, e sem ler a thumbnail: 0 `locate()` e 0 `FileResponse` em 550 revalidações |
| Busca com dispositivo desconectado devolve 200 e `absolute_path: null` | Sim, com `device.connected: false` e o rótulo — no nível 1 com duplos e no nível 2 com PostgreSQL e a enumeração real de volumes |
| Uma enumeração por disco distinto | Dez resultados em dois discos: duas chamadas a `mount_point()`; nada lembrado entre requisições |
| `GET /images/{id}` inexistente | 404 via `ImageNotFoundError` re-baseada |
| Todo erro de domínio pré-existente continua 400 | Sim, exceto `ImageNotFoundError`, a única reclassificação, nomeada e justificada (§4.2) |
| `thumbnail_backfill --root PATH` | Pula linhas com thumbnail, a menos que `--force`; não carrega o modelo (grafo de imports verificado num interpretador novo) |
| Custo de thumbnail por imagem, e impacto nas 2,2 img/s | 21,3 ms sozinha no demo (26,9 ms no pipeline, 6,0% da inferência; 2,21–2,28 → 2,09 img/s); 109 ms a 20 MP (114 ms no pipeline, 11,4%) (§7.2) |
| Tamanho médio da thumbnail | 45,2 KB no demo, ~53 KB nos sintéticos grandes; 4,3–5,0 GB por 100.000 (§9) |
| Latência dos guardas | 1,66 ms (404), 2,09 ms (403), 3,74 ms (204) em processo; criar um processo, 2,36 ms (§6) |
| Custo da revalidação | `304` em 6,6 ms contra `200` em 8,6 ms, sobre PostgreSQL em processo (§7.2) |
