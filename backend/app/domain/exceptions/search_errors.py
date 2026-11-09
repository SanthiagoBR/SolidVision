"""Exceções de domínio próprias da busca (RFC-025)."""

from app.domain.exceptions.domain_error import DomainError


class EmptySearchQueryError(DomainError):
    """Levantada quando uma consulta de busca não traz texto buscável.

    Uma consulta em branco é rejeitada, e não respondida. Não há ranking que
    faça sentido de todas as imagens contra nada, e o modelo de embedding
    codificaria `""` de bom grado num vetor que ranqueia o acervo por um
    acidente da torre de texto.
    """

    def __init__(self, message: str = "Search query cannot be empty.") -> None:
        super().__init__(message)


class InvalidSearchLimitError(DomainError):
    """Levantada quando o número de resultados pedido está fora do intervalo permitido.

    As duas pontas levantam exceção, em vez de cortar. Devolver em silêncio
    100 resultados a um chamador que pediu 10.000 -- ou 1 a um que pediu 0 --
    esconde a discordância em dados dos quais o chamador vai tirar
    conclusões, enquanto uma exceção a põe diante de quem escreveu a chamada.
    """

    def __init__(self, message: str = "Invalid search limit.") -> None:
        super().__init__(message)


class EmbeddingDimensionMismatchError(DomainError):
    """Levantada quando um vetor de consulta não casa com o espaço do índice.

    Comparar vetores de larguras diferentes não tem sentido, e não é vazio. A
    distinção importa porque a falha plausível é silenciosa: o `zip` do
    Python trunca no operando mais curto, então um laço de cosseno escrito à
    mão sobre uma consulta de 3 dimensões e linhas de 512 devolve um número
    de aparência perfeitamente razoável, calculado com três das 512
    dimensões. Toda implementação de repositório levanta esta exceção, para
    que um vetor do tamanho errado falhe em memória do mesmo jeito que falha
    contra uma coluna `vector(512)`.
    """

    def __init__(
        self, message: str = "Embedding dimension does not match the index."
    ) -> None:
        super().__init__(message)


class UnreadableQueryImageError(DomainError):
    """Levantada quando uma imagem enviada como consulta não pode ser decodificada.

    Ver RFC-038 §4.2. Bytes que não são uma imagem, um JPEG cortado, um
    formato diferente do declarado, ou mais pixels do que o decodificador
    aceita abrir. Recusada antes de o modelo vê-la: o adaptador deixa passar
    os erros de decodificação de propósito, e um que chegasse a ele seria um
    500 para o que é problema do arquivo do chamador.
    """

    def __init__(self, message: str = "The query image cannot be read.") -> None:
        super().__init__(message)
