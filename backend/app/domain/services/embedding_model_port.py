"""Contrato abstrato dos componentes que geram embeddings de objetos de domínio."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector


class EmbeddingModelPort(ABC):
    """Gera embeddings a partir de dados de domínio, sem alterar as entradas recebidas.

    As implementações precisam tratar os objetos de domínio recebidos como
    somente leitura, e não podem alterar a instância de Image nem a string de
    texto recebidas.
    """

    @abstractmethod
    def encode_image(
        self, image: Image, content: bytes | None = None
    ) -> EmbeddingVector:
        """Devolve um embedding para a imagem recebida.

        `content`, quando dado, são os bytes do arquivo, já lidos (RFC-045
        §5.3): a implementação os decodifica em vez de abrir o arquivo de
        novo, e precisa produzir exatamente o embedding que produziria a
        partir do arquivo. Quando `None`, a imagem é lida do local dela, como
        antes do RFC-045. Bytes crus, e não um tipo da camada Application,
        porque o Domain não importa nada acima dele. Uma implementação que
        não tem arquivo para ler -- um dublê de teste -- aceita o argumento e
        o ignora.
        """

    @abstractmethod
    def encode_text(self, text: str) -> EmbeddingVector:
        """Devolve um embedding para o texto recebido."""

    def encode_images(
        self,
        images: Sequence[Image],
        contents: Sequence[bytes | None] | None = None,
    ) -> list[EmbeddingVector]:
        """Devolve um embedding por imagem recebida, na ordem dada.

        Concreto, e não abstrato, e esse é todo o desenho. Uma implementação
        que tem um jeito mais rápido de codificar várias imagens de uma vez
        sobrescreve este; uma que não tem herda o laço abaixo e continua
        correta sem escrever uma segunda cópia de nada. Acrescentar mais um
        método obrigatório teria forçado toda implementação -- incluindo o
        dublê de teste -- a ganhar código para uma otimização que ela não
        tem.

        A alternativa que o RFC-024 considerou era um contrato opcional
        separado, que os chamadores sondariam em tempo de execução. Isso
        empurra uma verificação `isinstance` para a camada Application, que é
        um teste de tipo fazendo as vezes de contrato; a implementação padrão
        daqui dá a mesma propriedade de "as implementações podem ignorar
        isto" sem sonda e sem desvio.

        O resultado precisa ser igual a `[self.encode_image(image) for image
        in images]` dentro da tolerância de ponto flutuante. Codificar várias
        imagens juntas pode mudar *como* o trabalho é escalonado; nunca pode
        mudar *o que* é calculado.

        `contents`, quando dado, tem uma entrada por imagem -- os bytes já
        lidos, ou `None` para uma imagem a ser lida do local dela (RFC-045
        §5.3) -- e é entregue a `encode_image()` par a par. Um tamanho que não
        bate com o de `images` é erro do chamador e levanta `ValueError`
        antes de qualquer coisa ser codificada.

        Os erros se propagam. Uma falha em qualquer ponto do grupo aborta a
        chamada inteira, e é trabalho do chamador -- não deste método --
        descobrir qual imagem foi a responsável, porque só o chamador sabe se
        tentar de novo uma por uma vale o tempo que custa.
        """
        if contents is None:
            return [self.encode_image(image) for image in images]
        if len(contents) != len(images):
            raise ValueError(
                f"{len(contents)} contents were given for {len(images)} images"
            )
        return [
            self.encode_image(image, content)
            for image, content in zip(images, contents, strict=True)
        ]

    def warm_up(self) -> None:
        """Faz agora o que a primeira chamada de `encode_*` faria de outro jeito.

        Concreto e sem efeito por padrão, pela razão de `encode_images` ser
        concreto: uma implementação sem nada para carregar -- o fake, e
        qualquer adaptador futuro que já esteja residente -- não deveria ter
        de escrever um método dizendo isso.

        O RFC-029 §9.1 é a razão de ele existir. O executor de jobs faz isto
        **antes** de começar a consultar a fila, porque uma implementação
        cuja primeira chamada tem segundos de preparação a pagar (o RFC-026
        §9.1 mediu ~4,95 s para a que é distribuída, a frio) não tem lote de
        que informar progresso enquanto os paga. Um worker que pegasse um job
        antes o seguraria, calado, durante todo esse tempo -- e com um
        timeout de inatividade curto o bastante seria recolhido antes de ter
        feito qualquer coisa.

        Não pode levantar por uma razão passageira a que uma chamada
        posterior sobreviveria; um chamador pode tratar uma falha aqui como
        "tentar de novo sob demanda", e não como fatal.
        """
