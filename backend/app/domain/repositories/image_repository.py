"""Porta abstrata de repositório para as operações de persistência de imagens."""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping, Sequence

from app.domain.entities.image import Image
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_presence import (
    MissingImages,
    Presences,
    PurgedImages,
)
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.listing import (
    ListedImages,
    ListingOrder,
    ListingSummary,
)
from app.domain.value_objects.position import (
    BoundingBox,
    PositionCells,
    PositionReading,
)
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHits


class ImageRepository(ABC):
    """Contrato de repositório para gerenciar as entidades de domínio Image."""

    @abstractmethod
    def save(self, image: Image) -> None:
        """Persiste uma imagem nova no repositório.

        Mantido como o contrato original do RFC-015, de criar uma vez, usado
        por `IndexImageUseCase`. A indexação incremental usa `save_indexed()`
        no lugar dele.
        """

    @abstractmethod
    def get(self, image_id: ImageId) -> Image | None:
        """Busca uma imagem pelo identificador."""

    @abstractmethod
    def exists(self, image_id: ImageId) -> bool:
        """Diz se existe uma imagem com o identificador dado."""

    @abstractmethod
    def delete(self, image_id: ImageId) -> None:
        """Remove uma imagem do repositório."""

    @abstractmethod
    def list(self) -> list[Image]:
        """Devolve todas as imagens que o repositório conhece."""

    @abstractmethod
    def save_indexed(self, record: IndexingRecord) -> None:
        """Cria ou atualiza uma imagem junto com o embedding e os metadados.

        A linha inteira é escrita a partir do registro, data de captura
        incluída: `record.image.captured_at` e `capture_source` substituem o
        que estava gravado, mesmo quando são `None`. Isso é o certo para o
        único chamador, que chega aqui com bytes novos ou que de fato
        mudaram -- uma data lida dos bytes antigos não descreve mais o
        arquivo, e "nunca examinada" é o estado honesto até um scan ler os
        novos.

        O mesmo vale para `record.thumbnail_path` (RFC-030): escrito como
        veio, `None` incluído, porque uma thumbnail gerada dos bytes antigos
        não mostra mais o arquivo -- e para a posição (RFC-032): `latitude`,
        `longitude` e `position_source` vêm da entidade, `None` incluído,
        pela razão da data de captura.

        E a linha é **desmarcada** (RFC-046 §5.1): uma foto que acabou de ser
        indexada existe, seja o que for que um scan anterior não achou.
        """

    @abstractmethod
    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Cria ou atualiza muitas imagens como uma única unidade de trabalho.

        Tudo ou nada: ou todos os registros são persistidos, ou nenhum. É
        isso que faz o método valer a pena -- um commit em vez de N -- e é
        também o que o torna perigoso, já que uma única linha ruim descarta
        os embeddings de todas as outras linhas do lote, e cada um deles
        custou tempo real de inferência.

        Por isso, espera-se que os chamadores recorram a `save_indexed()`
        por registro quando este levanta, para que só a linha de fato ruim
        se perca (RFC-024 §7.2). Uma implementação precisa se deixar
        utilizável exatamente para isso: na falha, faz rollback limpo em vez
        de deixar estado aplicado pela metade.

        Violações de restrição se propagam como em `save_indexed()`; são
        erros reais, e não sinais de criação duplicada.
        """

    @abstractmethod
    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Devolve as imagens mais próximas de `embedding`, a melhor primeiro.

        A contraparte de `save_indexed()`: aquele método é como um embedding
        entra no repositório, e este é o único jeito de usá-lo depois que
        está lá. O ranking é do repositório -- os chamadores recebem uma
        lista ordenada e não devem reordená-la, já que uma implementação é
        livre para ranquear pelo que guarda, em vez de recalcular qualquer
        coisa no processo do chamador.

        O contrato que toda implementação deve cumprir:

        - Os resultados vêm do mais parecido ao menos parecido, e
          `SearchHit.similarity` é a similaridade de cosseno em [-1, 1].
          Nunca é reescalada para [0, 1]; uma nota negativa é uma resposta
          real, e quer dizer que os vetores apontam em sentidos opostos.
        - Uma imagem sem embedding gravado nunca aparece. Ela nunca foi
          comparada, e inventar uma nota para ela poria arquivos não
          indexados na frente dos indexados.
        - **Nem uma imagem marcada** (RFC-046 §6): o último scan do disco
          dela não achou o arquivo. As duas condições são uma regra só --
          *buscável* -- e toda leitura que serve a interface aplica as duas:
          esta, a listagem e o resumo dela, as duas contagens de fotos
          deixadas de fora e o mapa.
        - Voltam no máximo `limit` resultados, menos quando menos imagens
          têm embedding, e `[]` quando nenhuma tem. Um resultado curto quer
          dizer que o repositório ficou sem candidatos, nunca que ficou sem
          qualidade: filtrar por uma nota mínima não faz parte deste
          contrato.
        - Imagens igualmente parecidas voltam numa ordem estável e
          determinística, para que repetir uma busca repita o resultado.
        - Sem `filters`, a busca cobre todas as imagens que o repositório
          conhece. O RFC-025 escreveu que não havia "no scoping argument,
          by collection or otherwise" e deu a condição para acrescentar um:
          uma tabela real, uma chave estrangeira real e regras de posse. O
          RFC-027 entrega isso para *dispositivos*, que são um fato físico
          sobre onde os bytes estão, e não uma escolha de modelagem; por
          isso `filters` restringe por dispositivo -- e, desde o RFC-028,
          por data de captura, que é um fato gravado dentro do arquivo.
        - Um intervalo de datas de captura é semiaberto, `[start, end)`,
          sobre o `captured_at` no horário local da câmera. Uma imagem cuja
          data de captura é desconhecida nunca casa com um intervalo, por
          mais largo que seja: desconhecido nunca quer dizer "casa"
          (RFC-020). O conjunto de dispositivos e o intervalo se combinam
          com AND.
        - Um círculo (RFC-032) mantém as imagens cuja posição está a até
          `radius_m` metros do centro pela **distância de haversine
          exata**, a borda incluída, calculada com `EARTH_RADIUS_M`. Uma
          caixa delimitadora pode restringir as linhas antes, mas só como
          um pré-filtro que nunca tira uma linha que a distância manteria.
          Uma imagem cuja posição é desconhecida nunca casa com um círculo,
          por maior que seja -- as implementações em memória precisam dizer
          `is None` explicitamente, porque o Python levanta onde o SQL
          compara NULL como falso em silêncio. As três cláusulas se
          combinam com AND.
        - `filters=None` e `filters=SearchFilters()` querem dizer a mesma
          coisa: nenhuma restrição, e byte a byte a consulta que o RFC-025
          entregou. Nenhum dos dois pode ser lido como "um conjunto vazio de
          dispositivos", que não casaria com nada e transformaria um
          argumento padrão numa busca que devolve zero resultados em
          silêncio -- nem como um intervalo de datas sem limites, que
          descartaria em silêncio toda imagem sem data.
        - A `Image` de cada resultado traz o `captured_at` e o
          `capture_source`, para que um chamador possa mostrar por que uma
          foto casou com um filtro de data e quanto essa data vale -- e,
          desde o RFC-032, a `latitude`, a `longitude` e o
          `position_source`, pela mesma razão, quanto a um círculo.
        - Uma busca filtrada obedece a todas as regras acima, incluindo a
          ordem, a faixa do cosseno e pular imagens sem embedding. Ela
          restringe o conjunto de candidatos; não muda o ranking dentro
          dele.
        - Filtrar por um dispositivo que não tem imagens devolve `[]`, e
          filtrar por um id de dispositivo desconhecido não é erro. O
          conjunto é uma restrição, e não uma afirmação de que os membros
          dele existem.
        - Um `embedding` cuja largura não bate com a dos vetores indexados
          levanta `EmbeddingDimensionMismatchError`. É um erro do chamador,
          e não uma busca que por acaso não casa com nada, e precisa falhar
          igual em todas as implementações -- o bug plausível é uma
          implementação que trunca para o vetor mais curto e devolve um
          ranking confiante e sem sentido.

        Espera-se que `limit` seja um número positivo que o chamador já
        validou; a política de quão grande uma página pode ser é da camada
        Application, e não daqui.

        **Um filtro pode custar recall, e isso é medido, e não suposto.** O
        PostgreSQL não empurra um predicado arbitrário para dentro do
        percurso do grafo HNSW; ele aplica o filtro ao que o índice já
        devolveu, então um scan que explorou `ef_search` candidatos pode
        devolver *menos* que `limit` linhas enquanto existem mais linhas que
        casam. É o mesmo mecanismo que o RFC-025 §7.3 mediu por acidente,
        com tuplas mortas. O filtro se justifica pela utilidade -- "buscar
        só no disco que está na minha mão" -- e não pela velocidade (RFC-027
        §9.1).
        """

    @abstractmethod
    def get_embedding(self, image_id: ImageId) -> EmbeddingVector | None:
        """Devolve o embedding gravado de uma imagem, ou `None` (RFC-038 §4.1).

        A leitura por trás de *"Parecidas com esta"*: o vetor que a indexação
        já pagou, devolvido para ser ranqueado contra os outros com
        `search_similar()`. Reabrir o arquivo exigiria o disco dele plugado
        -- o caso para o qual o produto existe é o disco na gaveta -- e uma
        passada do CLIP para produzir esse mesmo vetor.

        `None` tanto quando não há linha quanto quando a linha não tem
        embedding. Distinguir os dois é do chamador, com `exists()`: são
        duas respostas diferentes para um usuário (404 e 409), e dobrar a
        segunda nesta assinatura faria todo chamador que só quer um vetor
        tratar um terceiro caso.

        Uma linha, uma coluna. `search_similar()` deixa a coluna fora do
        `SELECT` de propósito, e este método é o único lugar em que um vetor
        sai de volta do repositório.
        """

    @abstractmethod
    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """Devolve os metadados de sistema de arquivos gravados de uma imagem.

        Devolve `None` quando não existe linha para `image_id` (arquivo
        novo). Devolve `IndexMetadata(None, None)` quando a linha existe mas
        ainda não tem metadados -- os chamadores devem tratar isso como
        mudado, e não como inalterado.

        Inclui o `capture_source` (RFC-028), o `thumbnail_path` (RFC-030) e
        o `position_source` (RFC-032) da linha, nenhum dos quais é um sinal
        de mudança; ver `IndexMetadata`.
        """

    @abstractmethod
    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Devolve os metadados de sistema de arquivos de muitas imagens de vez.

        A contraparte em massa de `get_index_metadata()`, e a razão de o
        RFC-024 tê-la acrescentado: uma reindexação sobre uma coleção
        inalterada faz a pergunta de pular uma vez por arquivo descoberto,
        então a versão por arquivo transforma um scan de 100.000 imagens em
        100.000 idas e voltas *antes* de qualquer inferência. É um problema
        de Big-O no número de arquivos, independente de quão rápido o
        modelo é.

        Ids sem linha simplesmente não aparecem no resultado -- o mapa não
        é preenchido com valores `None`. Os chamadores precisam, portanto,
        distinguir "ausente" (arquivo novo) de "presente com campos `None`"
        (a linha existe, metadados nunca escritos) exatamente como fazem
        para a versão de um id só.
        """

    @abstractmethod
    def list_change_signals(self, device_id: DeviceId) -> dict[ImageId, ChangeSignals]:
        """Devolve o tamanho e a data de modificação de toda linha de um dispositivo.

        O instantâneo do RFC-045 §5.2: lido uma vez por job, na thread
        principal, antes de a thread de leitura antecipada começar, para que
        ela possa prever de quais arquivos a decisão incremental vai pedir
        os bytes sem tocar no banco -- uma sessão não é segura entre
        threads, e uma segunda conexão aberta de outra thread é o que o
        `deferring()` do RFC-036 §7.5 existe para evitar.

        **Uma previsão, e não a decisão.** O pipeline continua lendo os
        metadados por janela e decidindo com `plan_indexing()`. Um
        instantâneo desatualizado custa uma leitura a mais ou uma leitura
        pelo caminho, nunca uma resposta errada.

        Toda linha do dispositivo, com ou sem embedding, e os `None` de uma
        linha que nunca teve as duas colunas ficam como estão: a decisão os
        trata como mudados, e a previsão também precisa tratar. Ids de
        outros dispositivos não aparecem.
        """

    @abstractmethod
    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        """Atualiza os metadados de sistema de arquivos de uma linha, sem o embedding.

        A metade de escrita da verificação por hash de conteúdo
        (ARCHITECTURE.md 16, passo 3): quando o mtime ou o tamanho de um
        arquivo mudou mas os bytes não, o embedding gravado continua certo,
        e recalculá-lo seria puro desperdício. Só os metadados que guiam a
        *próxima* decisão de pular precisam se atualizar.

        Não faz nada quando não existe linha para `image_id`; um chamador
        que quer uma linha criada precisa passar por `save_indexed()`, que é
        o único contrato que leva um embedding.

        Escreve os três sinais de mudança e mais nada. Em particular, não
        toca na data de captura, diga o que disser `metadata.capture_source`:
        ela é de `update_capture_date()`, e uma atualização que também a
        escrevesse voltaria uma linha examinada para "nunca examinada"
        sempre que um chamador montasse o `IndexMetadata` sem ela. A posição
        fica intocada pela mesma razão, diga o que disser
        `metadata.position_source` (RFC-032 §8), e o local da thumbnail
        também -- uma atualização quer dizer que os bytes não mudaram,
        então a thumbnail ainda os mostra.
        """

    @abstractmethod
    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        """Grava a data de captura examinada de uma linha existente (RFC-028).

        Escreve `captured_at` e `capture_source` e mais nada -- nenhum
        embedding, nenhum sinal de mudança -- porque uma data de captura é
        um atributo buscável da fotografia, e não uma razão para
        reindexá-la (RFC-028 §6.1). É assim que uma imagem já indexada
        ganha uma data sem pagar por inferência.

        Recebe um `CaptureDate`, nunca `None`: não existe chamada que marque
        uma linha como "nunca examinada" de novo, já que nada legítimo
        desfaz a leitura de um arquivo.

        Não faz nada quando não existe linha para `image_id`, o mesmo
        contrato de `update_index_metadata()`. Uma data só vale a pena ser
        guardada ao lado de uma imagem que o sistema conhece, e criar uma
        linha aqui criaria uma sem embedding.

        Incondicional. Decidir *se* escreve -- só linhas nunca examinadas
        durante um scan; nunca trocar uma fonte mais forte por uma mais
        fraca sob o `--force` do backfill -- é a política do chamador, em
        `capture_date_to_write()`.
        """

    @abstractmethod
    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        """Grava muitas datas de captura numa escrita só.

        A contraparte em massa de `update_capture_date()`, pela razão de o
        RFC-024 ter acrescentado `get_index_metadata_many()`: o primeiro
        scan depois do RFC-028 data uma coleção inteira já indexada, e um
        comando por arquivo transformaria essa passada em 100.000 idas e
        voltas.

        Ids sem linha são pulados em silêncio, exatamente como na versão de
        uma linha. Um mapa vazio não escreve nada.
        """

    @abstractmethod
    def update_position(self, image_id: ImageId, reading: PositionReading) -> None:
        """Grava a posição examinada de uma linha existente (RFC-032 §8).

        Escreve `latitude`, `longitude` e `position_source` e mais nada --
        nenhum embedding, nenhum sinal de mudança, nenhuma data de captura
        -- porque uma posição é um atributo buscável da fotografia, e não
        uma razão para reindexá-la. É assim que uma imagem já indexada ganha
        uma posição sem pagar por inferência.

        Recebe um `PositionReading`, nunca `None`: nada legítimo desfaz a
        leitura de um arquivo, então não existe chamada que marque uma linha
        como "nunca examinada" de novo.

        Não faz nada quando não existe linha para `image_id`, o contrato de
        `update_capture_date()`.

        Incondicional. Decidir *se* escreve é a política do chamador, em
        `position_to_write()`.
        """

    @abstractmethod
    def update_position_many(self, readings: Mapping[ImageId, PositionReading]) -> None:
        """Grava muitas posições numa escrita só.

        A contraparte em massa de `update_position()`, usada uma vez por
        janela de prefetch pela razão de existir `update_capture_date_many()`:
        o primeiro scan depois do RFC-032 posiciona uma coleção inteira já
        indexada, e um comando por arquivo seriam 100.000 idas e voltas. Ids
        sem linha são pulados em silêncio; um mapa vazio não escreve nada.
        """

    @abstractmethod
    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        """Grava onde a thumbnail de uma linha existente foi guardada (RFC-030).

        A escrita do backfill de thumbnails: dá uma thumbnail a uma imagem
        já indexada sem tocar no embedding nem nos sinais de mudança dela,
        do jeito que `update_capture_date()` lhe dá uma data.

        Não faz nada quando não existe linha para `image_id`. Uma thumbnail
        só vale a pena ser apontada a partir de uma imagem que o sistema
        conhece, e criar uma linha aqui criaria uma sem embedding.
        """

    @abstractmethod
    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        """Grava muitos locais de thumbnail numa escrita só.

        A contraparte em massa de `update_thumbnail_path()`, usada pelo
        backfill uma vez por janela de prefetch pela razão de existir
        `update_capture_date_many()`. Ids sem linha são pulados em
        silêncio; um mapa vazio não escreve nada.
        """

    @abstractmethod
    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        """Conta as imagens buscáveis que um filtro de data escondeu por não terem data.

        O número por trás de "N fotos ficaram de fora porque a data é
        desconhecida". O RFC-028 §4.1 exige que a interface consiga dizer
        isso: sem ele, "não acho a foto de 2018" e "a foto de 2018 está
        indexada mas não tem EXIF" parecem iguais -- um resultado vazio que
        se lê como se a foto não existisse, que é a falha que o RFC-028
        §2.1 existe para remover.

        Conta as imagens que (a) têm embedding, então uma busca poderia
        tê-las devolvido, (b) satisfazem todas as *outras* cláusulas de
        `filters` -- o conjunto de dispositivos e, desde o RFC-032, o
        círculo -- e (c) têm `captured_at` NULL, seja nunca examinada, seja
        examinada sem data. Quando `filters.captured_between` é `None` não
        havia cláusula de data para esconder nada, a resposta é 0, e uma
        implementação precisa devolvê-la sem consultar o banco.

        Três propriedades para saber antes de "consertar" o que parece
        inconsistente:

        - É uma **segunda consulta**, e não um subproduto da busca. Uma
          busca vetorial devolve no máximo `limit` linhas e não tem como
          saber o que o `WHERE` dela descartou.
        - Conta sobre **a tabela inteira sob os filtros**, e não sobre a
          vizinhança que um índice aproximado por acaso explorou. É essa a
          pergunta que o usuário faz -- quantas fotos o filtro escondeu? --
          e não é, de propósito, o universo do ranking. Os dois números não
          precisam somar nada.
        - Ignora o texto da consulta. A relevância não é a razão de essas
          imagens terem ficado de fora; a data ausente é.
        """

    @abstractmethod
    def count_unknown_position(self, filters: SearchFilters) -> int:
        """Conta as imagens buscáveis que não têm posição (RFC-032 §6.2).

        O número por trás de "N fotos ficaram de fora porque não têm
        coordenadas". Sem ele, um círculo responde com a mentira mais cara
        que o produto pode contar -- *não há foto deste lugar* -- quando a
        verdade é *1.200 fotos não têm coordenadas e nenhuma delas foi
        olhada*.

        Conta as imagens que (a) têm embedding, (b) satisfazem todas as
        *outras* cláusulas de `filters` -- o conjunto de dispositivos e o
        intervalo de datas de captura -- e (c) têm `latitude` NULL, seja
        nunca examinada, seja examinada sem posição. `filters.taken_within`
        é ignorado: é a cláusula cujas vítimas estão sendo contadas.

        **Ao contrário de `count_unknown_capture_date()`, este não responde
        0 para um filtro sem círculo.** `GET /images/map` faz exatamente
        esta pergunta sem círculo nenhum (RFC-032 §7) -- quantas fotos o
        mapa não consegue desenhar -- e precisa ser o mesmo número sobre o
        mesmo universo. Não pagar pela consulta numa busca sem círculo
        continua sendo regra; ela é cumprida uma camada acima, onde
        `SearchImagesUseCase` nem chama este método.

        Valem as três propriedades de `count_unknown_capture_date()`: uma
        segunda consulta, sobre a tabela inteira sob os filtros, e não sobre
        a vizinhança que um índice aproximado explorou, ignorando o texto
        da consulta.
        """

    @abstractmethod
    def aggregate_positions(
        self,
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> PositionCells:
        """Agrupa as imagens buscáveis de `area` em células do mapa (RFC-032 §7).

        Um mapa não se desenha com resultados de busca: os 100 primeiros de
        um índice de 100.000 imagens não dizem nada sobre onde as fotos
        estão, e levar 100.000 pontos ao cliente para agrupá-los é levar a
        coleção pela rede para responder uma pergunta que `GROUP BY`
        responde aqui.

        O universo é **o dos filtros, e não o do ranking**: imagens com
        embedding, que satisfazem o conjunto de dispositivos e o intervalo
        de datas de captura de `filters`, cuja posição fica dentro de `area`
        (bordas incluídas). É o universo de `count_unknown_position()`
        restrito às imagens que têm posição -- os dois números descrevem uma
        população, dividida pelo que pode ser desenhado. Não há texto de
        consulta; ranquear é assunto da busca.

        Cada célula é o par de coordenadas arredondado para `precision`
        casas decimais, metade para longe do zero, **como o PostgreSQL
        arredonda um `double precision` convertido para `numeric`** -- os 15
        dígitos significativos do valor arredondados em decimal, e não o
        float binário arredondado pelo `round()` do Python. As
        implementações em memória reproduzem isso exatamente
        (`round_to_cell()`), ou uma foto na borda de uma célula cairia em
        células diferentes em implementações diferentes.

        Voltam no máximo `cell_limit` células -- todas quando é `None` --
        ordenadas por latitude e depois por longitude, para que uma resposta
        truncada continue determinística. Um chamador que precisa saber se a
        área tem *mais* de N células pede N + 1: o teto é a política do
        chamador (`MAX_MAP_CELLS`), e este método só para de contar depois
        dele.

        `filters.taken_within` não é consultado: a área do mapa é a janela
        visível, e "perto daqui" é outra pergunta (RFC-032 §9).
        """

    @abstractmethod
    def list_by_filters(
        self,
        filters: SearchFilters,
        order: ListingOrder,
        limit: int,
        after: Image | None,
    ) -> ListedImages:
        """Toda imagem buscável sob `filters`, em ordem de data de captura (RFC-044).

        A leitura por trás de *"Ver todas as fotos daqui, por data"*: sem
        consulta, sem modelo, sem nota -- o que existe aqui, no tempo. Uma
        página por vez: no máximo `limit` imagens, começando logo depois de
        `after` na ordem.

        O contrato que toda implementação deve cumprir, ao qual
        `test_listing_contract.py` prende as três:

        - **O universo é o da busca** (RFC-032 §7): imagens com embedding,
          sob o conjunto de dispositivos, o intervalo de datas e o círculo
          de `filters`, pelas mesmas cláusulas -- um intervalo nunca casa
          com uma data desconhecida, um círculo nunca casa com uma posição
          desconhecida (RFC-020), e `SearchFilters()` não restringe nada.
        - **Uma ordem total.** `captured_at` no sentido de `order`; depois
          `relative_path`, depois o id, os dois sempre crescentes. O caminho
          põe uma rajada no mesmo segundo na numeração da câmera
          (`DJI_0042`, `DJI_0043`) e as sem data na ordem da pasta; o id
          separa o mesmo caminho em dois discos, como desempata na busca
          (RFC-025 §6). Sem ele a ordem não seria total, e um cursor
          repetiria ou pularia uma das cópias.
        - **As sem data vêm depois de toda imagem com data, nos dois
          sentidos** (RFC-044 §3.2). Nenhuma foto sem data fica entre duas
          com data.
        - **Os caminhos se comparam por code point**, nas três
          implementações: a collation `C` no PostgreSQL, a comparação de
          `str` no Python -- que são a mesma ordem para texto UTF-8. `B`
          vem antes de `a`.
        - **`after` é uma posição na ordem, e não um membro da lista.** A
          página começa na primeira imagem cuja chave de ordenação passa da
          de `after`, satisfaça ou não o próprio `after` os `filters`, tenha
          ou não embedding. `None` é a primeira página.
        - Pedir página após página, cada `after` sendo a última imagem da
          página anterior, dá exatamente a lista pedida de uma vez: nenhuma
          imagem repetida, nenhuma pulada.
        - No máximo `limit` imagens, `[]` quando não sobra nada. Um chamador
          que precisa saber se há próxima página pede uma a mais
          (`limit + 1`), como o mapa pede `MAX_MAP_CELLS + 1` células: uma
          página curta não é o sinal.
        - Toda imagem traz o `captured_at`, o `capture_source`, a
          `latitude`, a `longitude` e o `position_source`, como um resultado
          de busca, e nenhum embedding é lido: os vetores ficam fora da
          consulta.

        Espera-se que `limit` seja positivo e já validado; a política de
        tamanho de página é da camada Application.
        """

    @abstractmethod
    def summarize_listing(self, filters: SearchFilters) -> ListingSummary:
        """O tamanho e o período da lista de `list_by_filters()` (RFC-044 §5.3).

        Sobre o mesmo universo, como um agregado só: quantas imagens,
        quantas delas sem data, e a primeira e a última datas de captura.
        Sob um intervalo de datas, `undated` é 0 -- uma data desconhecida
        nunca casa com um -- e numa lista vazia as contagens são 0 e as
        datas, `None`.

        As duas contagens de fotos deixadas de fora
        (`count_unknown_capture_date()`, `count_unknown_position()`) não
        fazem parte desta resposta: são da política de requisição da busca,
        que a listagem chama como está.
        """

    @abstractmethod
    def list_presence(self, device_id: DeviceId) -> Presences:
        """Devolve o id, o caminho e a marca de toda linha de um dispositivo (RFC-046).

        A leitura da varredura, ao fim de um job do disco inteiro (§5.5):
        uma consulta nesse momento, e não o instantâneo de
        `list_change_signals()`, que é do início do job, para que as linhas
        que o próprio job criou também sejam comparadas.

        Toda linha do dispositivo -- com ou sem embedding, marcada ou não --
        e nada de nenhum outro dispositivo. Sem ordem: o chamador procura
        ids. Só colunas nomeadas; os vetores ficam onde estão.

        **Em fluxo**, uma consulta lida em pedaços: o chamador a consome
        antes de escrever qualquer coisa, e nunca segura o dispositivo
        inteiro de uma vez.
        """

    @abstractmethod
    def mark_presence(
        self,
        missing: Collection[ImageId],
        present: Collection[ImageId],
        since: datetime.datetime,
    ) -> None:
        """Marca `missing` como ausentes desde `since` e desmarca `present` (RFC-046).

        **Uma transação**, as duas escritas ou nenhuma (§5.5, passo 4). A
        seção chama as duas metades de `mark_missing()` e `mark_present()`;
        são um método só para poderem ser uma transação só, o que um
        repositório que faz commit por chamada não poderia prometer de outro
        jeito.

        - Uma linha de `missing` que **já está marcada mantém a data**: o
          cartão diz desde quando as fotos não foram encontradas, e um scan
          posterior que de novo não as acha não torna isso mais recente.
        - Uma linha de `present` perde a marca, seja qual for a data.
        - Ids sem linha são pulados em silêncio, como em toda escrita em
          massa daqui. Coleções vazias não escrevem nada.

        O chamador mantém os dois conjuntos disjuntos; um id nos dois é
        recusado com `ValueError`, em vez de resolvido por uma ordem que
        ninguém escolheu.
        """

    @abstractmethod
    def count_missing_by_device(self) -> dict[DeviceId, MissingImages]:
        """Conta de vez as linhas marcadas de cada dispositivo, e a marca mais antiga.

        Um `GROUP BY` para todos os dispositivos, pela razão que
        `count_by_device()` dá (RFC-031 §4.3): `GET /devices` desenha todos
        os discos. Um dispositivo sem linha marcada **não aparece**, como
        `count_by_device()` omite um vazio; os chamadores o leem com
        `.get()`.
        """

    @abstractmethod
    def purge_missing(self, device_id: DeviceId) -> PurgedImages:
        """Apaga as linhas marcadas de um dispositivo, e diz o que saiu (RFC-046 §5.7).

        Um `DELETE ... RETURNING thumbnail_path`, numa transação: toda linha
        marcada de `device_id` e mais nada -- nenhuma linha visível, nenhuma
        linha de outro dispositivo. Devolve quantas linhas saíram e os
        locais de thumbnail que elas guardavam, para o store apagar
        **depois** que as linhas se foram: uma thumbnail sem linha é lixo no
        cache, e uma linha sem a thumbnail seria uma linha apontando para o
        nada.

        Irreversível, e é por isso que a rota que o chama recusa enquanto o
        disco tem um job, e a interface pergunta antes, com o número.
        """

    @abstractmethod
    def count_by_device(self) -> dict[DeviceId, int]:
        """Devolve quantas imagens cada dispositivo tem, numa leitura agrupada.

        **Um mapa, e não uma contagem para um id, e o plural é o
        contrato.** `GET /api/v1/devices` desenha todos os discos de uma
        vez, então uma versão de um dispositivo só seriam N consultas atrás
        de uma assinatura que parece uma -- o mesmo defeito de enumerar os
        volumes uma vez por dispositivo, uma camada abaixo (RFC-031 §4.3 do
        build prompt).

        Dispositivos sem imagens **não aparecem**, em vez de aparecerem com
        zero, do jeito que `get_index_metadata_many()` omite ids sem linha.
        Os chamadores o leem com `.get(device_id, 0)`; preenchê-lo aqui
        exigiria que este método soubesse quais dispositivos existem, que é
        a pergunta do outro repositório.

        Conta toda linha, tenha ou não embedding. O número responde *"quantos
        arquivos deste disco o sistema conhece"*, que é o que fica ao lado
        de `last_scan_file_count` na resposta, e uma linha sem embedding
        continua sendo um arquivo que este sistema conhece.

        **Menos uma linha marcada** (RFC-046 §6): um arquivo que o último
        scan não achou não é um que o sistema sabe estar no disco, e o
        numerador da porcentagem é o que a busca consegue achar. As linhas
        marcadas são de `count_missing_by_device()`.
        """

    @abstractmethod
    def count_by_path_prefixes(
        self, device_id: DeviceId, parent: JobScope
    ) -> dict[str, int]:
        """Conta as imagens por subpasta imediata de `parent`, numa consulta.

        A leitura por trás de `GET /api/v1/devices/{id}/folders`: uma
        listagem de pastas mostra `indexed_images` por linha, e quarenta
        linhas não podem ser quarenta `COUNT(*)` (RFC-031 §8.2).

        O resultado mapeia **um componente de caminho** -- o primeiro
        segmento abaixo de `parent` -- para o número de linhas em qualquer
        lugar abaixo dele. `parent` sendo o escopo do dispositivo inteiro
        agrupa pelo primeiro segmento do próprio `relative_path`.

        Duas propriedades que uma implementação precisa reproduzir
        exatamente, porque o teste de contrato fixa as duas e as
        implementações naturais divergem nelas:

        * **Imagens que estão direto em `parent` aparecem sob o próprio
          nome de arquivo.** `2018/loose.jpg` sob `parent="2018"` dá a chave
          `loose.jpg`, que é um arquivo e não uma pasta. Isso é deliberado,
          e não tolerado: filtrá-la aqui significaria este método decidir o
          que é diretório, o que ele não tem como saber sem tocar no disco.
          O chamador já tem os nomes de pasta que o sistema de arquivos
          informou e só mantém essas chaves, então essas entradas são
          descartadas uma camada acima, junto com o outro tipo de chave que
          não pode virar linha.
        * **O outro tipo: uma pasta renomeada no disco desde que foi
          indexada.** As linhas dela continuam guardadas sob o nome antigo,
          então o nome antigo volta aqui e não casa com nada que o chamador
          viu. Mesmo destino, mesma linha de código.

        A comparação é por texto exato num prefixo separado por `/`, que é
        o que `relative_path` guarda -- `ImagePath` grava a forma posix,
        então não há `\\` na coluna para tratar. O separador final no
        prefixo é essencial: sem ele `2018` casaria com `2018b`, que é outra
        pasta (RFC-029 §10).

        Uma subpasta sem imagens indexadas não aparece, em vez de aparecer
        com zero, pela razão de `count_by_device()` omitir um dispositivo
        vazio: os nomes vêm do chamador, e inventar chaves aqui seria
        adivinhá-los.

        Linhas marcadas não são contadas (RFC-046 §6), como em
        `count_by_device()`.
        """
