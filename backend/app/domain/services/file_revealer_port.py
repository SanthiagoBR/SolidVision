"""Mostrar um arquivo ao usuário no gerenciador de arquivos dele (RFC-030 §5).

Uma porta com um adaptador, de propósito. O RFC-030 §5.3 cita três
plataformas e entrega uma: `explorer /select,` no Windows está implementado e
testado, enquanto `open -R` no macOS e a chamada D-Bus FileManager1 no Linux
estão declarados e não escritos. Um adaptador que ninguém rodou é uma
promessa de portabilidade que ninguém verificou (RFC-027 §4.1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.image_path import ImagePath


class FileRevealerPort(ABC):
    """Abre o gerenciador de arquivos da plataforma com um arquivo selecionado."""

    @abstractmethod
    def reveal(self, path: ImagePath) -> None:
        """Pede ao gerenciador de arquivos para mostrar `path`, e volta ao lançá-lo.

        Voltar quer dizer que o pedido foi *lançado*, nunca que uma janela
        abriu: o `explorer.exe` sai com código diferente de zero mesmo quando
        dá certo, então nenhum status de saída aqui seria confiável para
        dizer mais que isso (RFC-030 §5.1).

        `path` nunca é algo que um cliente mandou. Ele é montado pelo
        servidor a partir de uma linha e de um ponto de montagem resolvido
        agora, que é toda a propriedade de segurança de `/reveal` -- as
        implementações não podem aceitar uma string, e nunca podem passá-la
        por um shell.

        Levanta `FileNotFoundError` quando não há nada em `path`, exatamente
        como abri-lo levantaria. Um gerenciador de arquivos a que se pede
        para selecionar um arquivo ausente não falha; o Explorer abre em
        silêncio uma pasta padrão no lugar, então, sem esta verificação, o
        usuário veria uma janela sem relação nenhuma e nenhum erro. O que um
        arquivo ausente *quer dizer* -- um 410, porque o disco está conectado
        e a foto não está nele -- é decisão do chamador, e é por isso que
        isto levanta o erro de I/O, e não um de domínio.
        """
