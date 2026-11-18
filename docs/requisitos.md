# SolidVision — Requisitos funcionais e não funcionais

Situação em 05/11/2026. Levantados da documentação do repositório: o [README], a [arquitetura][ARCHITECTURE], o [AI_Context], os [ADRs][índice dos ADRs], os [RFCs 001 a 046][índice dos RFCs] e os roadmaps do [frontend][ROADMAP-frontend], do [modo sem Docker][ROADMAP-standalone] e do [fine-tuning][ROADMAP-fine-tuning].

Este documento não decide nada novo. Ele reúne numa lista só o que esses documentos já pedem do sistema, diz onde cada requisito foi decidido e se foi verificado. Se um RFC e este documento divergirem, vale o RFC.

Os casos de uso que cobrem os requisitos funcionais, com os diagramas UML e a rastreabilidade de cada RF, estão em [casos-de-uso.md](casos-de-uso.md).

---

## 1. Contexto

**O problema.** Um fotógrafo profissional guarda centenas de milhares de fotos em HDs internos e externos, ao longo de anos, e precisa achar uma foto específica sem depender do nome do arquivo, da pasta ou de etiquetas manuais. O projeto nasceu com um fotógrafo de imagens aéreas.

**A solução.** Um aplicativo desktop local. Ele indexa as fotos onde elas estão, converte cada uma num vetor semântico (*embedding*) com o modelo CLIP e permite buscá-las por uma descrição em linguagem natural, por uma foto de exemplo, por data, por dispositivo e por lugar.

**Ator.** Um único usuário, o dono do acervo, na própria máquina Windows. Não há contas, perfis nem acesso remoto.

### Como ler

- **RF** é um requisito funcional (o que o sistema faz), **RNF** um não funcional (com que qualidade) e **RT** uma restrição técnica (o que foi fixado e não é opção).
- **Origem** aponta o documento que decidiu o requisito; § é a seção dele.
- **Os números não mudam.** Um requisito novo recebe o próximo número livre e entra na seção do assunto dele: RF-69 a RF-72 e RNF-73, do [RFC-046], estão junto dos requisitos com que se relacionam, e não no fim.
- **Situação**:

| Símbolo | Significado |
|---|---|
| ✅ | Implementado e verificado, por teste ou por medição registrada no RFC |
| ⚠️ | Implementado, com ressalva: a meta não foi atingida em todos os casos, ou a verificação foi simulada |
| ⏳ | Implementado; falta a verificação em condição real (ver [§6](#6-pendências-de-verificação)) |
| 🚧 | Em implementação: uma parte entregue e medida, o resto ainda não |
| 📋 | Proposto, ainda não implementado |

### Glossário

| Termo | Significado neste documento |
|---|---|
| Acervo | As fotos do usuário, nos discos dele. O sistema só as lê |
| Dispositivo | Um volume físico registrado: HD externo, cartão de memória, disco interno. É identificado pelo GUID do volume no Windows, e não pela letra da unidade |
| Indexar | Ler uma foto, extrair os metadados, gerar a miniatura e o embedding e gravar tudo no banco |
| Job | Um pedido de indexação de um dispositivo inteiro, ou de algumas pastas dele, executado em segundo plano |
| Embedding | O vetor de 512 números que representa o conteúdo de uma foto ou de um texto, os dois no mesmo espaço |
| Similaridade | O cosseno entre dois embeddings, de -1 a 1 |
| Círculo | O filtro de lugar: um centro e um raio em metros |
| Posição | A coordenada GPS gravada no EXIF da foto; num drone, a da aeronave, e não a do chão fotografado |

---

## 2. Visão geral

| Grupo | Requisitos |
|---|---|
| Funcionais | Dispositivos (RF-01 a RF-11 e RF-69), indexação (RF-12 a RF-26, RF-70 e RF-71), busca (RF-27 a RF-35 e RF-72), filtros (RF-36 a RF-40), resultados e detalhe (RF-41 a RF-45), mapa e lugares (RF-46 a RF-53), abrir e fechar o app (RF-54 a RF-60), instalação e configuração (RF-61 a RF-64), linha de comando e manutenção (RF-65 a RF-68) |
| Não funcionais | Desempenho e escala (RNF-01 a RNF-08), privacidade e segurança (RNF-09 a RNF-20), funcionamento sem rede e modelos (RNF-21 a RNF-24), confiabilidade (RNF-25 a RNF-34), integridade dos dados (RNF-35 a RNF-41 e RNF-73), qualidade da busca (RNF-42 e RNF-43), usabilidade (RNF-44 a RNF-47), instalação e portabilidade (RNF-48 a RNF-52), manutenibilidade (RNF-53 a RNF-59), testabilidade (RNF-60 a RNF-64), observabilidade (RNF-65 a RNF-68), licenças e políticas de uso (RNF-69 a RNF-72) |
| Restrições | RT-01 a RT-10 |

---

## 3. Requisitos funcionais

### 3.1 Dispositivos

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-01 | O sistema deve listar os volumes montados na máquina no momento da consulta, indicando quais já são dispositivos registrados. Um drive sem mídia aparece sem a opção de adicionar. | [RFC-031] §5, [RFC-039] §14 | ✅ |
| RF-02 | O usuário deve poder registrar um volume como dispositivo. O registro é feito pela identidade do volume, nunca por um caminho, e registrar o mesmo volume de novo devolve o mesmo dispositivo. | [RFC-027] §4, [RFC-031] §6 | ✅ |
| RF-03 | O registro deve exigir um rótulo (ex.: "HD2"), sugerido pela interface e sem repetição entre dispositivos. | [RFC-039] §5.4 | ✅ |
| RF-04 | O usuário deve poder renomear um dispositivo. O rótulo é o único campo editável. | [RFC-031] §7, [RFC-039] §5.5 | ✅ |
| RF-05 | O sistema deve reconhecer um disco já registrado mesmo quando a letra da unidade muda, sem reindexá-lo. | [RFC-027] §2, §4.1 | ⏳ |
| RF-06 | A barra lateral deve mostrar, para cada dispositivo, o rótulo, se está conectado, as fotos indexadas, o percentual indexado e o job ativo. | [RFC-031] §4, [RFC-039] §5.2 | ✅ |
| RF-07 | O percentual indexado deve vir sempre ao lado da data do scan que o originou. Sem esse scan, nenhum percentual é mostrado; o número é arredondado para baixo e nunca passa de 100%. | [RFC-027] §8, [RFC-031] §4.2, [RFC-039] §5.2 | ✅ |
| RF-08 | A interface deve mostrar a conexão e a desconexão de um disco sem erro genérico: na hora, pela mensagem de volume do Windows, e em até 10 s pelo polling. | [RFC-039] §5.3 | ⏳ |
| RF-09 | O usuário deve poder navegar pelas pastas de um dispositivo, um nível por vez, com trilha de navegação, na ordem do Explorer e com um filtro acima de 50 pastas. Cada pasta mostra o estado (*indexando*, *na fila*, *parcial — parou em …*, *indexada* ou *nunca indexada*) e quantas fotos tem indexadas, sem percentual por pasta. | [RFC-031] §8, [RFC-039] §5.6 | ✅ |
| RF-10 | Com o disco desconectado, a tela de pastas deve dizer que é preciso conectá-lo (*"Conecte o HD2 para ver as pastas"*). | [RFC-031] §8, [ROADMAP-frontend] Etapa 2 | ✅ |
| RF-11 | A tela inicial deve avisar sobre os dispositivos registrados e nunca indexados, com um atalho para escolher as pastas. | [RFC-037] §7, [RFC-039] §5.11 | ✅ |
| RF-69 | O usuário deve poder remover do índice as fotos que o último scan do disco inteiro não encontrou (RF-70). O cartão do dispositivo mostra quantas são e a data do scan (*"312 fotos não encontradas no scan de 12/03"*), e o botão *"Remover do índice"* pede confirmação com o número de fotos, fica desligado enquanto o disco tem um job e apaga as fotos e as miniaturas do índice, sem precisar do disco conectado e sem tocar nele. | [RFC-046] §5.7, §5.8 | ✅ |

### 3.2 Indexação

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-12 | O usuário deve poder indexar um dispositivo inteiro ou as pastas que selecionar nele. Na seleção, a pasta mãe absorve as filhas. | [RFC-029] §3, [RFC-039] §5.6 | ✅ |
| RF-13 | A indexação deve rodar em segundo plano, num processo separado da API: a API só registra o pedido, e o executor de jobs o processa. | [ADR-004], [RFC-029] §6 | ✅ |
| RF-14 | Deve haver no máximo um job ativo por dispositivo. Um segundo pedido para o mesmo disco é recusado como conflito (409). | [RFC-029] §9 | ✅ |
| RF-15 | O usuário deve poder acompanhar o progresso de um job: arquivos descobertos, processados, pulados e com falha, e o último arquivo salvo. A barra de progresso aparece só na indexação do disco inteiro; o job em si não exibe percentual. | [RFC-029] §7.2, [RFC-039] §5.7 | ✅ |
| RF-16 | O usuário deve poder cancelar um job. O cancelamento é cooperativo, e a interface mostra *"cancelando…"* até o job parar. Um job ainda na fila é cancelado na hora; cancelar um job já terminado é recusado (409). | [RFC-029] §8 | ✅ |
| RF-17 | Um job interrompido, por cancelamento, pelo fechamento do app ou pela morte do processo, deve poder continuar do último ponto salvo, sem perder nem repetir fotos. | [RFC-029] §10, [RFC-036] §16 | ✅ |
| RF-18 | A indexação deve ser incremental. Um arquivo com o mesmo tamanho e a mesma data de modificação é pulado; o hash SHA-256 só é calculado quando um dos dois mudou; o embedding só é refeito quando o conteúdo mudou. | [RFC-020], [RFC-024] §4, [ARCHITECTURE] §16 | ✅ |
| RF-19 | O sistema deve indexar arquivos JPG, JPEG, PNG, TIFF, BMP e WEBP. A lista é configurável (`SUPPORTED_EXTENSIONS`). | [.env.example], [RFC-024] | ✅ |
| RF-20 | O sistema deve gerar o embedding de cada foto com o modelo CLIP, em lotes. | [RFC-023], [RFC-024] §5 | ✅ |
| RF-21 | O sistema deve extrair a data de captura do EXIF (`DateTimeOriginal` e, na falta dele, `DateTimeDigitized`) e registrar de onde ela veio. A data de modificação do arquivo nunca é usada como data de captura. | [RFC-028] §4 | ✅ |
| RF-22 | O sistema deve extrair a posição GPS do EXIF, na mesma leitura da data, e registrar de onde ela veio. A coordenada 0, 0 ("sem sinal de satélite") conta como desconhecida. | [RFC-032] §4 | ✅ |
| RF-23 | O sistema deve distinguir uma foto cujo metadado nunca foi examinado de uma que foi examinada e não tem o dado. | [RFC-028] §4.2, [RFC-032] §4.4 | ✅ |
| RF-24 | O sistema deve gerar uma miniatura de cada foto indexada (512 px no lado maior, configurável) e guardá-la no cache do aplicativo. | [RFC-030] §7 | ✅ |
| RF-25 | Ao listar a raiz de um disco inteiro, o job deve gravar quantos arquivos encontrou e quando: é o denominador do percentual de RF-07. | [RFC-039] §4.1 | ✅ |
| RF-26 | Quando o executor de jobs não consegue subir, a interface deve mostrar a faixa *"A indexação está indisponível"*, com o motivo, e desabilitar *"Indexar"*. | [RFC-039] §5.8 | ✅ |
| RF-70 | Ao fim de um job do disco inteiro concluído, o sistema deve marcar como ausentes as fotos indexadas que a listagem do disco não encontrou e desmarcar as que voltaram, sem gerar o embedding de novo. Marcar não apaga: a foto, o vetor e a miniatura ficam no banco até o usuário removê-los (RF-69). Um job de algumas pastas não marca nada (RNF-73). | [RFC-046] §3, §4, §5.4, §5.5 | ✅ |
| RF-71 | Toda varredura deve deixar de fora o cache de miniaturas do aplicativo, mesmo dentro de uma pasta indexada, e, na raiz do volume, a lixeira (`$RECYCLE.BIN`) e a `System Volume Information`. Uma foto apagada pelo Explorer sai da busca como uma apagada de vez. | [RFC-030] §7.1, [RFC-046] §5.6 | ✅ |

### 3.3 Busca

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-27 | O usuário deve poder buscar fotos por uma descrição em linguagem natural, em português ou em inglês. A consulta em português é traduzida para o inglês antes de chegar ao modelo. | [RFC-023] §7, [RFC-025], [RFC-026] | ✅ |
| RF-28 | Os resultados devem vir ordenados por similaridade, com desempate determinístico, e a grade deve mostrá-los na ordem exata da resposta. | [RFC-025] §6, [RFC-038] §8 | ✅ |
| RF-29 | A busca deve mostrar 40 resultados por vez, e *"Mostrar mais"* até o máximo de 100. Pela API, o padrão é 10. | [RFC-026] §5, [RFC-038] §5.2 | ✅ |
| RF-30 | A busca deve recusar uma consulta vazia ou só de espaços, e um limite fora de 1 a 100, com uma mensagem clara. | [RFC-026] §5.3 | ✅ |
| RF-31 | O usuário deve poder buscar as fotos parecidas com uma foto já indexada (*"Parecidas com esta"*). A busca usa o embedding guardado: funciona sem carregar o modelo e com o disco desconectado. A própria foto sai do resultado. | [RFC-038] §4.1 | ✅ |
| RF-32 | O usuário deve poder buscar por uma foto de exemplo, soltando-a na barra de busca (JPEG ou PNG, até 50 MB). A foto não é guardada: o arquivo temporário é apagado ao fim de toda requisição. | [RFC-038] §4.2, §5.5 | ✅ |
| RF-33 | A busca deve rodar quando o usuário pressiona Enter, e não a cada tecla. O texto e os filtros ficam na URL da tela. | [RFC-038] §5.1, §5.2 | ✅ |
| RF-34 | A interface deve mostrar o estado do modelo de busca: *"Carregando o modelo de busca…"* enquanto ele carrega, e o motivo quando estiver indisponível. | [RFC-037] §7 | ✅ |
| RF-35 | A tela inicial deve oferecer consultas de exemplo e um resumo do acervo: o número de dispositivos, as fotos indexadas e a data da última indexação. | [RFC-037] §7 | ✅ |
| RF-72 | As fotos marcadas como ausentes (RF-70) não devem aparecer na busca, na listagem por data, no mapa nem nas contagens dos dispositivos. O detalhe de uma foto marcada continua abrindo, e o Explorer diz que o arquivo não está mais no disco. | [RFC-046] §3, §5.8, §6 | ✅ |

### 3.4 Filtros

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-36 | O usuário deve poder filtrar por um ou mais dispositivos. | [RFC-027] §9, [RFC-038] §5.2 | ✅ |
| RF-37 | O usuário deve poder filtrar por intervalo de datas de captura, que são as do relógio da câmera, sem fuso. O sistema informa quantas fotos o filtro excluiu por não terem data. | [RFC-028] §8 | ✅ |
| RF-38 | O usuário deve poder filtrar por proximidade, com um círculo: um centro e um raio entre 300 m, 1 km, 5 km, 10 km, 25 km e 100 km (1 km quando ninguém escolheu), nunca abaixo do mínimo configurado. O sistema informa quantas fotos o filtro excluiu por não terem posição. | [RFC-032] §6, [RFC-038] §5.2, [RFC-040] §2.4 | ✅ |
| RF-39 | O centro do círculo deve poder vir de uma foto (*"Buscar perto daqui"*), de um ponto do mapa, de coordenadas digitadas ou de um endereço (RF-52). | [RFC-038] §5.4, [RFC-040] §5.4, [RFC-043] §8.5 | ✅ |
| RF-40 | Os filtros devem se combinar entre si (E lógico) e valer para a busca por texto, por foto de exemplo e por foto parecida, e para a listagem por data. | [ARCHITECTURE] §14, [RFC-038] §4.5, [RFC-044] §5.1 | ✅ |

### 3.5 Resultados e detalhe da foto

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-41 | Cada cartão de resultado deve mostrar a miniatura, o rótulo do dispositivo, o nome do arquivo, se o disco está desconectado e a similaridade como número: o cosseno com três casas, nunca como percentual. | [RFC-038] §2.10, §5.3 | ✅ |
| RF-42 | As miniaturas devem aparecer mesmo com o disco desconectado. Uma foto sem miniatura aparece como um quadro com o nome do arquivo, e não como erro. | [RFC-030] §7, [RFC-038] §5.3 | ✅ |
| RF-43 | O detalhe de uma foto deve mostrar a miniatura, a data de captura e a origem dela, a posição e a origem dela, o dispositivo, o caminho dentro do disco e, com o disco conectado, o caminho completo. Um resultado num disco desconectado é uma resposta completa (*"está no HD3, em fotos/2018"*), e não um erro. | [RFC-030] §4, [RFC-038] §5.4 | ✅ |
| RF-44 | O usuário deve poder abrir o Explorer com a foto selecionada (*"Mostrar no Explorer"*), quando a função está habilitada e o disco está conectado. Se o disco não está conectado, ou o arquivo não está mais nele, a interface diz qual dos dois. | [RFC-030] §5, [RFC-038] §5.4 | ✅ |
| RF-45 | O usuário deve poder copiar o caminho da foto. | [RFC-038] §5.4 | ✅ |

### 3.6 Mapa e lugares

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-46 | O sistema deve ter uma tela de mapa com a contagem de fotos por célula na área visível, numa precisão que acompanha o zoom. Acima de 2 000 células a grade engrossa, e a resposta diz a precisão aplicada; ela nunca trunca. | [RFC-032] §7, [RFC-040] §5.3 | ✅ |
| RF-47 | O mapa deve aplicar os filtros de dispositivo e de data e ignorar o texto da busca: ele mostra onde estão as fotos que os filtros permitem. | [ARCHITECTURE] §14, [RFC-040] §5.5 | ✅ |
| RF-48 | O mapa deve avisar quantas fotos ficaram de fora por não terem posição, ou por não terem data quando há filtro de datas. | [RFC-040] §4.1, §5.5 | ✅ |
| RF-49 | Ao clicar numa célula, um balão deve mostrar a contagem e as ações *"Buscar perto daqui"* (no menor raio da lista que cobre a célula), *"Aproximar"* e *"Ver todas as fotos daqui, por data"*. | [RFC-040] §5.4, [RFC-044] §6.2 | ✅ |
| RF-50 | O mapa deve ter uma legenda, com cinco classes fixas de contagem, e uma lista das 20 células mais cheias. | [RFC-040] §5.3, §5.5 | ✅ |
| RF-51 | Com rede, o fundo do mapa deve mostrar os tiles do OpenStreetMap, com a atribuição à vista e um controle para desligá-los. Sem rede, as células aparecem sobre o contorno dos 27 estados, que vai no instalador. | [RFC-040] §5.2 | ⚠️ |
| RF-52 | O usuário deve poder digitar um endereço e levar até ele o mapa, ou o centro do *"Perto de"*. A interface mostra o nome do lugar encontrado e avisa que o endereço é enviado ao OpenStreetMap (Nominatim). A função pode ser desligada (`ALLOW_ADDRESS_SEARCH`). | [RFC-043] §8 | ✅ |
| RF-53 | O usuário deve poder ver todas as fotos de um círculo pela data de captura, sem consulta de texto: as mais recentes primeiro, *"Mais antigas primeiro"* a um clique, as sem data no fim, depois de um divisor, e *"Mostrar mais"* até a última página. Funciona mesmo com a busca indisponível, porque não usa o modelo. | [RFC-044] | ✅ |

### 3.7 Abrir, acompanhar e fechar o app

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-54 | Abrir o app deve subir sozinho tudo o que ele usa (o banco, as migrations, a API e o executor de jobs) e mostrar as fases da subida. Na primeira execução, a fase aparece como *"Preparando o banco pela primeira vez"*. | [RFC-036] §5, [RFC-037] §6 | ✅ |
| RF-55 | O app deve ter uma instância só: abri-lo de novo traz a janela existente para a frente, sem subir um segundo backend. | [RFC-036] §8, [RFC-037] §6.1 | ✅ |
| RF-56 | Uma falha na subida deve mostrar uma tela de erro com a causa e um botão *"Abrir a pasta de logs"*. | [RFC-037] §6.3 | ✅ |
| RF-57 | Se a API reiniciar, a janela deve mostrar *"Reconectando…"*, e não uma página em branco, e voltar sozinha. | [RFC-037] §6.3 | ✅ |
| RF-58 | O sistema deve supervisionar os próprios processos e reiniciar o que morrer, com espera crescente e um limite de tentativas. Uma falha que reiniciar não conserta (código de saída 78) não é repetida. | [RFC-036] §6 | ✅ |
| RF-59 | Fechar a janela deve encerrar tudo, em ordem. Com um job em andamento, o app avisa antes que a indexação pausa e continua na próxima abertura. | [RFC-037] §6.4, [ROADMAP-frontend] D3 | ✅ |
| RF-60 | Encerrar a sessão do Windows (logoff) com o app aberto deve fazer a mesma parada ordenada. | [RFC-036] §7.5 | ⏳ |

### 3.8 Instalação e configuração

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-61 | O sistema deve ser distribuído num instalador único (`SolidVision-Setup-0.1.0.exe`) que traz o Python, o PostgreSQL e os modelos de IA, instala por usuário sem pedir administrador, cria atalhos no Menu Iniciar e na área de trabalho e abre o app ao terminar. | [RFC-041] §7 | ⏳ |
| RF-62 | A desinstalação, por *Configurações > Aplicativos*, deve funcionar com o app aberto ou fechado: ela para o app e a indexação, remove o programa e os atalhos e **preserva os dados**, de modo que reinstalar reaproveita o acervo indexado. | [RFC-041] §7.4 | ⏳ |
| RF-63 | O usuário deve poder ajustar a configuração num arquivo opcional, `%LOCALAPPDATA%\SolidVision\solidvision.env`, com as mesmas chaves do `.env.example`. | [RFC-034] §5.3 | ✅ |
| RF-64 | A API deve informar à interface o que a instalação suporta (o Explorer, a busca por endereço, o raio mínimo e o limite máximo da busca), para que ela só mostre o que funciona. | [RFC-031] §9, [RFC-038] §4.4, [RFC-043] §8.1 | ✅ |

### 3.9 Linha de comando e manutenção

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RF-65 | O sistema deve oferecer uma linha de comando para indexar uma pasta (`index --root --label`) e buscar por texto (`search`) e por imagem (`search-image`). | [README] §4, [RFC-021], [RFC-029] §12 | ✅ |
| RF-66 | O launcher deve poder ser usado pela linha de comando: subir tudo, consultar o estado (`status`) e parar (`stop`). | [RFC-036] §4.3 | ✅ |
| RF-67 | O sistema deve permitir completar, sem carregar o modelo, as miniaturas e os metadados EXIF (data e posição) de fotos indexadas antes dessas funções existirem, com as opções `--force` e `--dry-run`. | [RFC-030] §7.2, [RFC-032] §8.1 | ✅ |
| RF-68 | A API deve ter uma rota de saúde que consulte o banco de verdade, respondendo 503 quando ele cai, e informe o estado da busca (`loading`, `ready`, `unavailable` ou `on_demand`). | [RFC-008], [RFC-036] §5.2 | ✅ |

---

## 4. Requisitos não funcionais

### 4.1 Desempenho e escala

As metas são as da [arquitetura][ARCHITECTURE] §22. As medições foram feitas na máquina dos RFCs (Windows 10 Home, Pentium G4560, 16 GB de RAM, HDD), a quente, salvo indicação. Os RFCs 045 e 046 mediram em outra (Windows 11 Home, Core i5-1035G1, 7,7 GB de RAM, NVMe), com o acervo real num HD externo pela USB.

| ID | Requisito | Medido | Origem | Situação |
|---|---|---|---|---|
| RNF-01 | A busca por texto deve responder em menos de 1 s. | ~110 ms em inglês e ~450 ms em português, com a tradução, no dataset de demonstração; da tecla Enter à grade pintada, 544 ms. **Com 100 000 fotos em português, ~0,7 s no banco mais ~0,45 s da consulta: acima de 1 s** | [ARCHITECTURE] §22, [RFC-025] §11.6, [RFC-038] §13, §14.4 | ⚠️ |
| RNF-02 | A indexação deve processar mais de 1 foto por segundo, em CPU. | 1,0 a 1,3 fotos/s na máquina dos RFCs; 2,2 com o PostgreSQL do Docker. Na do RFC-045, com a leitura antecipada, 3,7 fotos/s (0,269 s por foto) na primeira indexação do HD externo: 8,7 h para as 116 396 fotos, contra 12,4 h sem ela | [ARCHITECTURE] §22, [RFC-024], [RFC-035], [RFC-045] §13 | ✅ |
| RNF-03 | A subida a quente deve levar menos de 5 s. | 4,46 s do launcher ao pronto; 6,42 s da janela à tela inicial; cerca de 1 min na primeira abertura, que cria o banco | [ARCHITECTURE] §22, [RFC-036] §16, [RFC-037] §15, [RFC-041] | ⚠️ |
| RNF-04 | As rotas que não usam IA devem responder em menos de 100 ms. | `/devices`, ~87 ms com 100 000 fotos; a primeira página das fotos de um lugar, 49 ms no servidor e 142 ms pela API | [ARCHITECTURE] §22, [RFC-031] §16, [RFC-044] §15 | ⚠️ |
| RNF-05 | O sistema deve suportar acervos de pelo menos 100 000 fotos. | Com 100 000: busca exata em 685 a 714 ms no banco; mapa do Brasil inteiro em 1 057 ms na janela; fotos de um lugar em 142 ms. Na máquina do RFC-046, a varredura das fotos ausentes nas 115 051 do HD externo em 4,1 a 4,2 s e 31 MB, e o filtro delas sem custo mensurável na busca | [ARCHITECTURE] §2, §22, [RFC-038] §14.4, [RFC-040] §13, [RFC-044] §15, [RFC-046] §15 | ✅ |
| RNF-06 | A interface não deve travar com listas grandes. | Uma pasta com 5 000 subpastas: 861 ms do clique à lista, sem tarefa longa na página | [RFC-039] §13 | ✅ |
| RNF-07 | A memória usada na indexação não deve depender da resolução das fotos: no máximo uma imagem decodificada em tamanho real por vez, com o tamanho do lote configurável (`BATCH_SIZE`). | Garantido por teste estrutural | [RFC-024] §9 | ✅ |
| RNF-08 | Varrer de novo um disco sem mudanças não deve ler as fotos inteiras, refazer embeddings nem gravar no banco: custa o `stat` e a leitura do cabeçalho EXIF de cada arquivo. | 45 fotos: 23,3 s na primeira indexação e 0,024 s na nova varredura; com as datas de modificação tocadas, 0,29 s (75× menos que reindexar). A frio, na máquina do RFC-046, as 115 051 fotos do HD externo em 2,3 h: 72,7 ms por foto, o `stat` e o cabeçalho | [RFC-020], [RFC-024] §4.3, [RFC-028] §6, [RFC-046] §15 | ✅ |

### 4.2 Privacidade e segurança

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RNF-09 | As fotos nunca saem da máquina: não há upload nem cópia delas. O banco guarda só metadados, embeddings e o lugar de cada miniatura. | [ARCHITECTURE] §2, [ADR-003] | ✅ |
| RNF-10 | O sistema nunca escreve nos discos do acervo. As miniaturas ficam no diretório de dados do app, que a varredura ignora mesmo quando ele está dentro de uma pasta indexada. | [AI_Context], [RFC-030] §7.1 | ✅ |
| RNF-11 | O texto das buscas nunca é gravado, nem no log, que guarda só o comprimento. Não existe histórico de buscas, e a janela tira a *query string* de toda URL que vai para o log dela. | [RFC-026] §16.1, [RFC-038] §2.7 | ✅ |
| RNF-12 | O endereço digitado na busca por endereço nunca é gravado em log nem em disco, e só é enviado quando o usuário pede, sem autocompletar. | [RFC-043] §8.3 | ✅ |
| RNF-13 | A API e o banco devem escutar só em `127.0.0.1`. | [RFC-033] §8.2, [RFC-036] §5.4 | ✅ |
| RNF-14 | A API deve recusar requisições cujo `Host` não seja `127.0.0.1` ou `localhost` (proteção contra *DNS rebinding*) e escritas vindas de outra origem (`Origin`). | [RFC-037] §5.2 | ✅ |
| RNF-15 | Nenhuma rota aceita um caminho de arquivo: toda ação sobre uma foto recebe o id dela, e o servidor monta o caminho. O Explorer é aberto com uma lista de argumentos, nunca por um shell. | [RFC-030] §5.1 | ✅ |
| RNF-16 | As rotas que agem na máquina (abrir o Explorer, a busca por endereço e as capacidades) só atendem chamadas da própria máquina. O Explorer também depende de um ajuste próprio, e os dois guardas são independentes. | [RFC-030] §6, [RFC-031] §9.1, [RFC-043] §8.1 | ✅ |
| RNF-17 | O banco embarcado deve usar autenticação SCRAM, com uma senha aleatória por instalação, nunca escrita em log, num diretório acessível só ao usuário e ao sistema. | [RFC-033] §6, §7.2, [RFC-034] §7.7 | ✅ |
| RNF-18 | O ambiente do usuário não deve mudar o comportamento do app instalado: as variáveis `PG*` são removidas antes de conectar ao banco, o ambiente do processo não é fonte de configuração, o Python embutido ignora os pacotes do usuário e três fusíveis do Electron (`RunAsNode` e dois de depuração) ficam desligados. | [RFC-034] §5.2, §7.5, [RFC-041] §6.3, §14 | ✅ |
| RNF-19 | O único tráfego que sai da máquina deve ser opcional e visível: os tiles do mapa e a busca por endereço, os dois com o User-Agent do projeto. | [ARCHITECTURE] §5, [RFC-040] §5.6, [RFC-043] §8.3 | ✅ |
| RNF-20 | A busca por foto de exemplo deve validar o que recebe: o tipo é obrigatório e conferido pelos primeiros bytes (415), o tamanho tem teto de 50 MB (413) e a imagem é decodificada antes de chegar ao modelo (400). | [RFC-038] §4.2 | ✅ |

### 4.3 Funcionamento sem rede e integridade dos modelos

| ID | Requisito | Medido | Origem | Situação |
|---|---|---|---|---|
| RNF-21 | O sistema deve funcionar sem rede: indexar e buscar (por texto em português e em inglês, e por imagem) numa máquina que nunca acessou o Hugging Face, sem nenhuma tentativa de conexão. | Numa instalação simulada, com a rede trocada por um sentinela: zero tentativas de conexão. Com a rede desligada de verdade, só numa máquina que já tinha o cache do Hugging Face | [RFC-035] §15 | ⚠️ |
| RNF-22 | Os modelos devem ser carregados de uma pasta local, fixados por revisão e `sha256` num lockfile e conferidos antes de carregar. Uma falha de integridade dá uma mensagem que diz o modelo, o arquivo e o que fazer. | — | [RFC-035] §4, §6 | ✅ |
| RNF-23 | Os embeddings devem ser reprodutíveis: iguais bit a bit aos de referência, conferidos por um teste e, no build do instalador, pelo Python do próprio pacote. | — | [RFC-035] §7, [RFC-041] §7.3 | ✅ |
| RNF-24 | O banco deve conter vetores de um único modelo. Trocar o modelo é uma decisão de projeto (um RFC), e não um ajuste, e exige reindexar todos os discos. O mesmo vale para qualquer mudança que altere os vetores: a decodificação reduzida do RFC-045 recomeçou o índice do zero, mantendo os dispositivos. | — | [ADR-007], [RFC-035] §7, [ARCHITECTURE] §11, [RFC-045] §5.9 | ✅ |

### 4.4 Confiabilidade e recuperação

| ID | Requisito | Medido | Origem | Situação |
|---|---|---|---|---|
| RNF-25 | Uma foto com defeito não deve interromper a indexação. Se um lote falha, as fotos dele são refeitas uma a uma, e só a defeituosa é reportada, com o caminho e o erro original. | — | [RFC-024] §6 | ✅ |
| RNF-26 | Uma falha ao gerar a miniatura não deve impedir que a foto seja indexada e encontrada. | — | [RFC-030] §7.2 | ✅ |
| RNF-27 | O ponto de retomada de um job nunca deve passar à frente do que já foi gravado, e a ordem de varredura deve ser a mesma entre execuções (a ordem de caminhos do Windows). Errar para trás custa reler alguns arquivos; errar para a frente perderia fotos. | — | [ARCHITECTURE] §16, [RFC-029] §10 | ✅ |
| RNF-28 | Um job cujo executor morreu deve voltar sozinho para a fila, com o ponto de retomada, até 3 tentativas. | — | [RFC-029] §9.1 | ✅ |
| RNF-29 | O banco deve se recuperar de um encerramento sujo (processo morto, máquina desligada) sem intervenção e sem perder transações confirmadas. | 12 mortes forçadas sob escrita, todas recuperadas em 4 a 8 s | [RFC-033] §9, §18 | ✅ |
| RNF-30 | Um processo do app que morre deve ser recuperado sozinho. | A API de volta em 4,6 s; o executor de jobs substituído em 12,9 s; a morte do banco percebida em 1,7 s | [RFC-036] §16 | ✅ |
| RNF-31 | Fechar o app não deve deixar nenhum processo para trás, conferido pela lista de processos do sistema. | Nenhum, em 27 aberturas; do fechar ao fim, 1,68 s | [RFC-036] §16, [RFC-037] §15 | ✅ |
| RNF-32 | A primeira criação do banco deve ser atômica, sem deixar um banco pela metade. Um banco mais novo que o código faz o app recusar a subida, em vez de mexer nele. | — | [RFC-033] §7.1, §7.4 | ✅ |
| RNF-33 | Uma configuração inválida deve impedir a subida, com uma mensagem que nomeia o arquivo e a chave. Uma chave desconhecida ou aposentada é avisada no log, e nunca usada em silêncio. | — | [RFC-034] §5.4, §5.5 | ✅ |
| RNF-34 | As operações concorrentes sobre jobs devem ser seguras: a regra de um job ativo por dispositivo é imposta por um índice único do banco, a reivindicação de um job é atômica e toda escrita depende do estado lido. | — | [RFC-029] §9 | ✅ |

### 4.5 Integridade e correção dos dados

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RNF-35 | O sistema de arquivos é a fonte da verdade, e o banco é só um índice dele: uma foto que sai do disco sai da busca no scan seguinte do disco inteiro. | [ADR-003], [AI_Context], [RFC-046] §1 | ✅ |
| RNF-36 | A identidade de uma foto deve ser estável, derivada do dispositivo e do caminho dentro dele. A letra da unidade, o ponto de montagem e o estado de conexão nunca são gravados: são resolvidos no momento do uso. | [RFC-027] §5, §7 | ✅ |
| RNF-37 | A data de captura deve ser guardada sem fuso, como a câmera a gravou, para que nenhuma foto mude de dia. | [RFC-028] §5 | ✅ |
| RNF-38 | O banco deve recusar meia coordenada e coordenadas fora do planeta, venham de onde vierem. | [RFC-032] §5, [ARCHITECTURE] §15 | ✅ |
| RNF-39 | A miniatura de uma foto sobrescrita não deve ser servida desatualizada: a revalidação é pelo hash do conteúdo (`ETag`). | [RFC-030] §7 | ✅ |
| RNF-40 | A similaridade deve ser publicada como é, o cosseno de -1 a 1, sem arredondar, reescalar ou limitar. | [RFC-025] §9, [RFC-026] §5.2 | ✅ |
| RNF-41 | Os números mostrados não devem esconder o que o sistema não sabe: o percentual vem com a data do scan, as fotos ausentes vêm com a data do scan que não as encontrou, os filtros dizem quantas fotos excluíram por falta de dado, e o job não inventa um percentual. | [RFC-027] §8, [RFC-028] §8.2, [RFC-032] §6.2, [RFC-039] §5.7, [RFC-046] §5.8 | ✅ |
| RNF-73 | Uma foto só deve ser dada como ausente por uma listagem completa do disco: nunca por um job de algumas pastas, cancelado ou com falha, nem com o disco desplugado ou em outro ponto de montagem, nem dentro de uma pasta que não pôde ser lida, que vai para o log do job como aviso. Na dúvida, a foto continua visível. | [RFC-046] §3, §4, §5.2, §5.4 | ✅ |

### 4.6 Qualidade da busca

| ID | Requisito | Medido | Origem | Situação |
|---|---|---|---|---|
| RNF-42 | A qualidade da busca deve ser medida num conjunto de referência, com pisos de regressão nos testes. | Sobre 45 imagens e 25 consultas: Recall@5 de 84,0% em inglês e 88,0% em português; Recall@1 de 64,0% e 56,0%; Recall@10 de 96,0% nas duas | [RFC-022], [RFC-025] §11 | ✅ |
| RNF-43 | Uma melhora da busca para fotografia aérea deve ser medida antes de treinar: primeiro um conjunto de avaliação maior, depois os modelos prontos de sensoriamento remoto, e só então a adaptação do CLIP. | Conjunto de 525 fotos aéreas do Wikimedia Commons, cada uma com uma consulta revisada à mão, congelado: 178 consultas de validação e 347 de teste, ainda fechado. Na validação, o modelo atual acha a foto entre as 5 primeiras em 83,7% das consultas em inglês e 78,1% em português; nenhum modelo de sensoriamento remoto o supera além do ruído, de ~±6 pontos (o GeoRSCLIP empata, o RemoteCLIP perde de 25 a 40 pontos); a tradução custa 5,6 pontos no Recall@5 e 8,4 no Recall@1. Faltam as legendas de treino, o conjunto de regressão e o treino | [RFC-042] §14, §15, [ROADMAP-fine-tuning] | 🚧 |

### 4.7 Usabilidade

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RNF-44 | A interface deve ser em português do Brasil, com mensagens de erro próprias para cada situação, em vez das mensagens da API, que são em inglês. | [RFC-038] §5.7, [RFC-039] §5.10, [ROADMAP-frontend] | ✅ |
| RNF-45 | Toda espera e toda falha devem ter um estado próprio na tela (vazio, sem resultados, modelo carregando, disco desconectado, indexação indisponível, reconectando), nunca uma página em branco ou um erro genérico. | [RFC-037] §6.3, §7, [RFC-038] §5.3, [RFC-039] §5.8 | ✅ |
| RNF-46 | O mapa deve poder ser usado pelo teclado, pela lista das células (RF-50). | [RFC-040] §5.5 | ✅ |
| RNF-47 | O visual deve seguir o mockup do Figma, com os mesmos tokens de design: cores, tipografia, raios e espaçamentos. | [RFC-037] §8 | ✅ |

### 4.8 Instalação e portabilidade

| ID | Requisito | Medido | Origem | Situação |
|---|---|---|---|---|
| RNF-48 | O app deve rodar no Windows 10 ou 11, 64 bits, sem Docker, sem rede e sem administrador. | Conferido na pasta do build, com a raiz de dados vazia; falta a conta limpa | [RFC-041] §9, [ADR-008] | ⏳ |
| RNF-49 | O programa e os dados devem ficar na conta de quem instalou, cada um numa pasta: `%LOCALAPPDATA%\Programs\SolidVision` e `%LOCALAPPDATA%\SolidVision`. O app não grava nada fora da raiz de dados. | Instalação simulada: nada gravado fora da raiz de dados | [RFC-034] §6, §16, [RFC-035] §5.4, [RFC-041] §4 | ✅ |
| RNF-50 | Caminhos com espaço e acento, como `C:\Users\João Silva`, devem funcionar. | — | [RFC-033] §10 | ✅ |
| RNF-51 | O tamanho do pacote deve ser medido e declarado. | Instalador de 1,03 GB; 2,2 GB instalado, dos quais 881 MB são os modelos | [RFC-041] §15, [RFC-035] §15 | ✅ |
| RNF-52 | Nenhum caminho dentro do pacote pode passar do limite de 260 caracteres do Windows, e o build confere isso. | — | [RFC-041] §2.5, §7.3 | ✅ |

### 4.9 Manutenibilidade e arquitetura

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RNF-53 | O backend deve seguir a Clean Architecture em quatro camadas (Presentation, Application, Domain e Infrastructure), com as dependências apontando para o Domain, que não importa nenhum framework, banco ou biblioteca de IA. Um teste de arquitetura confere isso. | [RFC-001], [RFC-015], [ARCHITECTURE] §5 a §7 | ✅ |
| RNF-54 | O modelo de IA deve ser substituível sem mudar o Domain nem a Application, atrás da porta `EmbeddingModelPort`. Isso já aconteceu uma vez, do SigLIP para o CLIP, sem mudar nenhuma linha dessas camadas. | [RFC-013], [RFC-023] §5 | ✅ |
| RNF-55 | O acesso à persistência deve passar por portas do Domain (*Repository Pattern*), e todas as implementações, a do PostgreSQL e as em memória, cumprem o mesmo teste de contrato. | [ADR-006], [RFC-010], [RFC-025] §10.2 | ✅ |
| RNF-56 | Nenhuma configuração fica fixa no código: tudo vem de um `Settings` tipado e injetado, e só o módulo de configuração lê variáveis de ambiente. | [RFC-004], [ARCHITECTURE] §17 | ✅ |
| RNF-57 | Toda mudança no banco deve ser feita por uma migration versionada, com `downgrade()`. | [RFC-007] | ✅ |
| RNF-58 | As dependências dos três runtimes (Python, PostgreSQL e modelos) devem ser fixadas por lockfiles com hash, e as da interface pelo `package-lock.json`. | [RFC-033] §5.1, [RFC-035] §4.1, [RFC-041] §5.2, [RFC-037] §3 | ✅ |
| RNF-59 | Cada mudança deve ser descrita num RFC numerado, antes da implementação, e atualizada com as medições depois. Nenhum número sem medição, e toda medição declara a máquina e as condições. | [Convenções dos RFCs] | ✅ |

### 4.10 Testabilidade e qualidade do código

| ID | Requisito | Medido | Origem | Situação |
|---|---|---|---|---|
| RNF-60 | As regras de negócio devem ser testáveis sem PostgreSQL, PyTorch, Hugging Face ou FastAPI, com um modelo falso e determinístico. Os testes com os modelos reais (`slow`) e com servidores PostgreSQL reais (`embedded`) são opcionais. | — | [ARCHITECTURE] §20, [RFC-014] | ✅ |
| RNF-61 | O código deve passar pelo Ruff, pelo Black e pelo `mypy --strict` no backend, e pelo ESLint, pelo Prettier e pela checagem de tipos na interface. | — | [RFC-003], [RFC-037] §4 | ✅ |
| RNF-62 | Os tipos da interface devem ser gerados do esquema OpenAPI da API, e as duas suítes de teste devem acusar qualquer divergência. | — | [RFC-037] §4.3 | ✅ |
| RNF-63 | A suíte de testes deve passar inteira. | Em 26/10/2026: 3 158 testes do backend na suíte padrão, mais 96 nos marcadores opcionais, e 369 da interface, todos passando | [ARCHITECTURE] §20 | ✅ |
| RNF-64 | O build do instalador deve falhar se o pacote não for o que a suíte testou: a árvore, o lockfile, o orçamento de caminho, as DLLs, o conteúdo do `app.asar` e os vetores do CLIP. | — | [RFC-041] §7.3 | ✅ |

### 4.11 Observabilidade

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RNF-65 | Cada programa deve ter o próprio arquivo de log, na pasta de logs da raiz de dados, com uma rotação que não perde registros. | [RFC-005], [RFC-034] §8 | ✅ |
| RNF-66 | As operações longas devem registrar o progresso periodicamente, e um lote que precisou ser refeito é registrado como aviso. | [ARCHITECTURE] §19, [RFC-024] §6, §11 | ✅ |
| RNF-67 | As rotinas não devem inundar o log: o `/health`, consultado a cada 2 s, não grava nada quando responde 200. | [RFC-037] §5.3 | ✅ |
| RNF-68 | A janela deve registrar os marcos de cada abertura num log próprio (`shell.log`), sem dados do usuário nas URLs: nem consultas, nem endereços, nem a área do mapa. | [RFC-037] §6.5, [RFC-038] §2.7, [RFC-040] §2.7 | ✅ |

### 4.12 Licenças e políticas de uso

| ID | Requisito | Origem | Situação |
|---|---|---|---|
| RNF-69 | O código e a documentação são CC0 1.0. Os componentes de terceiros que o instalador redistribui têm a licença listada em `THIRD-PARTY-NOTICES.md`, que vai no instalador e é conferido no build. | [README] Licença | ✅ |
| RNF-70 | O uso dos tiles do OpenStreetMap deve seguir a política deles: atribuição à vista, um User-Agent próprio, nenhum download em massa e nada guardado para uso sem rede. | [RFC-040] §2.6 | ✅ |
| RNF-71 | O uso do Nominatim deve seguir a política deles: no máximo uma consulta por segundo no app inteiro, o User-Agent do projeto, um cache das respostas em memória, nenhum autocompletar, a atribuição e o serviço trocável por configuração (`ADDRESS_SEARCH_URL`). | [RFC-043] §8.3 | ✅ |
| RNF-72 | As imagens do dataset de demonstração devem ter a licença e a atribuição registradas, e não vão no instalador. | [RFC-022] §8, [README] Licença | ✅ |

---

## 5. Restrições técnicas

| ID | Restrição | Por quê | Origem |
|---|---|---|---|
| RT-01 | Só Windows 10 ou 11, 64 bits. As outras plataformas ficam declaradas atrás das portas, sem implementação. | A identidade de volume (GUID) e o *"Mostrar no Explorer"* são do Windows | [RFC-027] §4.1, [RFC-030] §5.3 |
| RT-02 | Inferência só em CPU. | A GPU não foi medida e não é pré-requisito para o app funcionar | [ROADMAP-standalone], [ROADMAP-frontend] |
| RT-03 | Backend em Python 3.12, com FastAPI, SQLAlchemy 2 e Alembic. | — | [ARCHITECTURE] §4 |
| RT-04 | PostgreSQL 16.15 com pgvector 0.8.6, embarcado e iniciado pelo próprio app. O Docker é só do desenvolvimento, na mesma versão maior, com codificação UTF8 e localidade C. | O pgvector disponível para Windows está preso à libpq 16; um fotógrafo não deve precisar instalar o Docker | [ADR-008], [RFC-033a] |
| RT-05 | Nenhuma extensão de banco além do `vector`, sem PostGIS. A distância é calculada pela fórmula de haversine sobre duas colunas. | Nada mais é necessário para um círculo | [RFC-032] §5.1 |
| RT-06 | No PostgreSQL 16 a busca é exata, comparando todas as fotos que passam nos filtros. O índice HNSW existe, e só é usado no PostgreSQL 17. | O desempate por id exige uma ordenação incremental que o 16 não faz sobre o índice | [RFC-038] §14.1 |
| RT-07 | Modelo CLIP `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`, de 512 dimensões, com o tradutor `Helsinki-NLP/opus-mt-ROMANCE-en` e o prompt `a photo of {query}`. | Escolhido numa comparação de quatro modelos: tão preciso quanto o SigLIP no corpus de demonstração e cerca de 10× mais rápido para indexar em CPU | [RFC-023], [ADR-001] |
| RT-08 | Interface em Electron 44, React 19, TypeScript, Vite, TanStack Query, React Router e Leaflet, com CSS Modules e sem framework de CSS ou de componentes; Node 24 LTS no desenvolvimento. | — | [RFC-037] §3, [RFC-040] |
| RT-09 | A interface é servida pela própria API, na mesma origem, sem CORS. | Evita uma configuração a mais e uma requisição de verificação a cada escrita | [ROADMAP-frontend] D2, [RFC-037] §5.1 |
| RT-10 | Instalador NSIS sem assinatura de código: o Windows SmartScreen avisa na primeira execução. | A assinatura ficou fora do escopo | [RFC-041] §7.5 |

---

## 6. Pendências de verificação

O que está implementado, mas ainda não foi verificado na condição que o requisito pede.

| O quê | Requisitos | Por que está pendente | Origem |
|---|---|---|---|
| Um HD externo de verdade: registrar, indexar duas pastas, cancelar e indexar de novo; desplugar com a tela de pastas aberta; trocar a letra da unidade | RF-05, RF-08 | A máquina das medições não tinha um HD externo. O fluxo foi ensaiado no `C:`, pela interface | [RFC-039] §13 |
| Instalar, abrir, indexar e buscar numa conta limpa do Windows, com a rede desligada; a primeira abertura a frio; a desinstalação | RF-61, RF-62, RNF-48 | Exige criar uma segunda conta do Windows. A pasta do build chegou à tela inicial nesta conta, com a raiz de dados vazia | [RFC-041] §9, §15 |
| Funcionar com a rede desligada numa máquina sem o cache do Hugging Face | RNF-21 | Feito numa instalação simulada e numa máquina com o cache | [RFC-035] §15 |
| O mapa sem rede de verdade | RF-51 | Simulado: só a janela perdeu o acesso aos tiles | [RFC-040] §13 |
| O logoff com o app aberto e um job em andamento | RF-60 | A verificação manual não foi feita | [RFC-036] §17 |
| A busca em português abaixo de 1 s com 100 000 fotos | RNF-01 | ~0,7 s no banco mais 0,45 s da consulta. Guardar o vetor dentro da linha da tabela (`STORAGE PLAIN`) mediu ~0,25 s no banco, mas a mudança pede um RFC e uma migration | [RFC-038] §14.4 |
| A avaliação da busca no conjunto maior, até o fim | RNF-42, RNF-43 | Com as 25 consultas do demo, cada uma vale 4 pontos percentuais: o Recall@5 em inglês tem intervalo de confiança de 95% de 65% a 94%. O conjunto do RFC-042 já existe, e a validação, com 178 consultas, distingue diferenças de ~6 pontos; o teste, com 347, só abre na comparação final do treino, e o conjunto de regressão em consultas gerais ainda não existe | [README] Métricas, [RFC-042] §14, §15 |

---

## 7. Fora do escopo e limitações conhecidas

Ficaram de fora por decisão, cada uma registrada no documento indicado:

- **Apagar um dispositivo e reescanear sob demanda** ([RFC-031] §10).
- **Uma tela que liste as fotos ausentes antes de removê-las, a remoção automática por prazo e a marcação das ausentes num job de algumas pastas** ([RFC-046] §12). As ausentes são marcadas só pelo job do disco inteiro e removidas só pelo botão do cartão (RF-69, RF-70).
- **Reaproveitar o vetor de uma foto movida.** Uma pasta movida deixa de aparecer em dobro, mas o caminho novo é indexado de novo, com inferência: reorganizar o HD externo inteiro custaria uma indexação inteira, 8,7 h ([RFC-046] §13).
- **Monitorar as pastas automaticamente** ([ARCHITECTURE] §23).
- **Coleções e vários modelos ao mesmo tempo.** `Collection` é só um marcador no código ([RFC-027] §10, [ADR-005]).
- **Histórico de buscas.** Só com um RFC próprio e desligado por padrão ([RFC-026] §16.1).
- **Autenticação, vários usuários, acesso remoto e sincronização na nuvem** ([RFC-026] §16, [ARCHITECTURE] §23).
- **Uma proteção contra programas locais mal-intencionados** (o token por sessão). A conferência do `Host` e do `Origin` fecha o acesso pelo navegador, e não por um programa da própria máquina ([ROADMAP-frontend]).
- **A foto em tamanho original no detalhe.** O detalhe usa a miniatura de 512 px ([ROADMAP-frontend]).
- **GPU, Linux e macOS** ([ROADMAP-frontend]).
- **Outros idiomas na interface além do português** ([ROADMAP-frontend]).
- **Ícone na bandeja e iniciar com o Windows** ([ROADMAP-frontend]).
- **Assinatura de código, atualização automática, migração entre versões do app e `pg_upgrade`** ([ROADMAP-standalone], [RFC-041] §11).
- **OCR, reconhecimento facial, detecção de duplicatas, app para celular e workers distribuídos** ([ARCHITECTURE] §23).

[README]: ../README.md
[ARCHITECTURE]: ../ARCHITECTURE.md
[AI_Context]: ../AI_Context.md
[.env.example]: ../.env.example
[índice dos ADRs]: adr/README.md
[índice dos RFCs]: rfcs/README.md
[Convenções dos RFCs]: rfcs/README.md#convenções
[ROADMAP-frontend]: rfcs/ROADMAP-frontend.md
[ROADMAP-standalone]: rfcs/ROADMAP-standalone.md
[ROADMAP-fine-tuning]: rfcs/ROADMAP-fine-tuning.md
[ADR-001]: adr/adr-001-modelo-de-embedding.md
[ADR-003]: adr/adr-003-imagens-permanecem-no-sistema-de-arquivos.md
[ADR-004]: adr/adr-004-indexacao-separada-do-http.md
[ADR-005]: adr/adr-005-metadados-de-embedding.md
[ADR-006]: adr/adr-006-repository-pattern.md
[ADR-007]: adr/adr-007-dimensao-fixa-por-coluna.md
[ADR-008]: adr/adr-008-postgresql-embarcado.md
[RFC-001]: rfcs/rfc-001-estrutura-do-projeto.md
[RFC-003]: rfcs/rfc-003-ambiente-python.md
[RFC-004]: rfcs/rfc-004-configuracao-tipada.md
[RFC-005]: rfcs/rfc-005-logging.md
[RFC-007]: rfcs/rfc-007-alembic.md
[RFC-008]: rfcs/rfc-008-health-api.md
[RFC-010]: rfcs/rfc-010-porta-de-repositorio.md
[RFC-013]: rfcs/rfc-013-porta-de-embedding.md
[RFC-014]: rfcs/rfc-014-fake-embedding-model.md
[RFC-015]: rfcs/rfc-015-casos-de-uso.md
[RFC-020]: rfcs/rfc-020-metadados-incrementais.md
[RFC-021]: rfcs/rfc-021-worker-de-indexacao.md
[RFC-022]: rfcs/rfc-022-dataset-de-demonstracao.md
[RFC-023]: rfcs/rfc-023-adaptador-de-embedding-clip.md
[RFC-024]: rfcs/rfc-024-pipeline-de-embeddings.md
[RFC-025]: rfcs/rfc-025-busca-semantica.md
[RFC-026]: rfcs/rfc-026-api-de-busca.md
[RFC-027]: rfcs/rfc-027-dispositivos-e-identidade-de-volume.md
[RFC-028]: rfcs/rfc-028-data-de-captura-e-filtro-temporal.md
[RFC-029]: rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md
[RFC-030]: rfcs/rfc-030-acesso-ao-arquivo.md
[RFC-031]: rfcs/rfc-031-dispositivos-e-pastas-pela-api.md
[RFC-032]: rfcs/rfc-032-geolocalizacao-e-busca-por-proximidade.md
[RFC-033]: rfcs/rfc-033-postgres-embarcado.md
[RFC-033a]: rfcs/rfc-033a-linha-de-base-do-postgres-embarcado.md
[RFC-034]: rfcs/rfc-034-configuracao-e-diretorios-do-usuario.md
[RFC-035]: rfcs/rfc-035-modelos-offline.md
[RFC-036]: rfcs/rfc-036-launcher-e-supervisao-de-processos.md
[RFC-037]: rfcs/rfc-037-fundacao-e-casca-do-frontend.md
[RFC-038]: rfcs/rfc-038-busca.md
[RFC-039]: rfcs/rfc-039-dispositivos-e-indexacao.md
[RFC-040]: rfcs/rfc-040-mapa.md
[RFC-041]: rfcs/rfc-041-empacotamento-e-instalador.md
[RFC-042]: rfcs/rfc-042-fine-tuning-do-clip.md
[RFC-043]: rfcs/rfc-043-busca-por-endereco-no-mapa.md
[RFC-044]: rfcs/rfc-044-fotos-por-localizacao-sem-consulta-textual.md
[RFC-045]: rfcs/rfc-045-leitura-antecipada-e-decodificacao-reduzida.md
[RFC-046]: rfcs/rfc-046-fotos-ausentes-do-disco.md
