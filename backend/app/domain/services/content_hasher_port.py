"""Contrato abstrato para calcular uma impressão digital dos bytes de uma imagem."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities.image import Image


class ContentHasherPort(ABC):
    """Calcula uma impressão digital estável dos bytes para os quais uma `Image` aponta.

    Espelha `EmbeddingModelPort`: a camada Application depende deste
    contrato, nunca do acesso ao sistema de arquivos de que uma implementação
    precisa para cumpri-lo. Ler um arquivo é I/O, e I/O é da Infrastructure.

    A impressão digital devolvida é só um valor de *detecção de mudança*.
    Nunca pode ser usada para identidade, deduplicação ou busca de linha: o
    `ImageId` deriva do caminho de propósito (RFC-022 §7.1), então dois
    arquivos idênticos byte a byte em dois caminhos são duas imagens
    distintas que por acaso têm a mesma impressão digital.

    As implementações precisam tratar a `Image` recebida como somente
    leitura, e deixar os erros de I/O se propagarem em vez de devolver um
    sentinela -- um chamador que não consegue ler os bytes precisa decidir o
    que isso quer dizer, e só ele sabe se a resposta certa é "pular este
    arquivo" ou "parar".
    """

    @abstractmethod
    def hash_image(self, image: Image) -> str:
        """Devolve a impressão digital dos bytes no local resolvido da imagem.

        As implementações leem `Image.require_absolute_path()`. Desde o
        RFC-027 uma imagem grava onde mora no *dispositivo* dela, e uma
        imagem cujo dispositivo está desplugado não tem bytes de que tirar a
        impressão digital -- isso levanta `DeviceNotConnectedError` em vez
        de devolver um sentinela, pela mesma razão de os erros de I/O se
        propagarem.
        """
