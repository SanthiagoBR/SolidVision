"""Perguntar ao sistema de arquivos onde está um dispositivo, sem importá-lo.

Três perguntas que a Application precisa conseguir fazer -- *este disco está
plugado agora?*, *esta pasta está mesmo nele?* e, desde o RFC-031, *que
pastas estão direto dentro dele?* -- e as três são perguntas para o sistema
operacional. O adaptador que as responde é `WindowsVolumeIdentityProvider`,
que é uma ABC da Infrastructure que a Application é proibida de importar
(`test_application_architecture.py` quebra o build se ela importar), então a
dependência é invertida aqui.

*O que está ligado a esta máquina?* de propósito **não** está aqui. Toda
assinatura abaixo recebe um `Device`, porque toda pergunta abaixo é sobre um
disco que o sistema já conhece; perguntar o que está plugado é perguntar
sobre volumes que ainda não são um dispositivo, e isso tem porta própria
(`VolumeCatalog`).

**Nada pode guardar cache entre chamadas.** O ponto de montagem de um
dispositivo é um fato deste instante, e todo o RFC-027 §7 é que nenhum valor
gravado consegue guardá-lo -- o usuário puxa o cabo e nada avisa este
processo. Uma implementação que lembrasse uma resposta entregaria a um job
uma letra de unidade que agora pertence a outro disco.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.value_objects.job_scope import JobScope


@dataclass(frozen=True)
class FolderEntry:
    """Uma pasta direto dentro de outra, e se ela tem alguma subpasta.

    `has_children` viaja com o nome, em vez de ficar para o chamador
    descobrir, porque descobrir quer dizer **abrir a pasta**: uma listagem de
    quarenta subpastas custa quarenta e uma chamadas de `readdir`, e não uma,
    e só o adaptador pode pagar isso sem a Application tocar num sistema de
    arquivos (RFC-031 §4.4 do build prompt).

    Ele está aqui porque uma interface que adivinhasse erraria no sentido
    visível: uma seta de expandir desenhada numa pasta folha é um erro que o
    usuário só descobre ao clicar nela.
    """

    name: str
    """O nome da própria pasta, como o sistema de arquivos o escreve.

    Um único componente de caminho, nunca um caminho. O chamador o junta à
    grafia canônica da pasta-mãe, e é isso que faz do resultado algo que um
    cliente pode mandar de volta direto como escopo de um job.
    """

    has_children: bool
    """Se esta pasta contém pelo menos uma subpasta.

    `False` para uma pasta que a plataforma se recusou a abrir -- uma
    barreira de permissão, um disco que sumiu no meio da listagem. Uma
    implementação registra isso no log e segue, em vez de derrubar a listagem
    inteira, que é a mesma regra que o RFC-027 aplica a um volume que não
    informa o nome.
    """


class DeviceLocator(ABC):
    """Porta para localizar os arquivos de um dispositivo no momento da pergunta."""

    @abstractmethod
    def mount_point(self, device: Device) -> Path | None:
        """Devolve onde `device` está montado agora, ou `None` se em lugar nenhum.

        `None` é a resposta para um disco na gaveta, e é uma resposta de
        primeira classe, e não um erro: o sistema conhece o dispositivo
        perfeitamente, só não tem bytes a oferecer (RFC-027 §2.3). Os
        chamadores a transformam em `DeviceNotConnectedError` quando
        precisavam dos bytes.

        O caminho devolvido nunca é gravado por ninguém. Ele vale até o
        usuário desplugar o disco, e é exatamente por isso que `Device` não
        tem campo para ele.
        """

    @abstractmethod
    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        """Devolve `scope` como o sistema de arquivos o escreve, ou `None` se ausente.

        Faz duas coisas que não podem ser feitas acima desta camada, e as
        duas são a razão de a porta ter este método, e não um `exists()`
        simples:

        * confirma que o escopo resolve para um diretório que está de fato
          *dentro* do dispositivo. `JobScope` já recusou `..`, uma âncora de
          unidade e um caminho UNC, mas uma junção NTFS dentro do escopo
          aponta para outro lugar inteiramente e parece uma pasta comum até
          ser resolvida (RFC-029 §7.1);
        * devolve a grafia canônica da pasta. Caminhos do Windows não
          diferenciam maiúsculas de minúsculas, então `Fotos` e `fotos` são
          um diretório só -- e `normalize_scopes()` compara as partes
          exatamente, então, sem a canonicalização aqui, duas grafias da
          mesma pasta sobreviveriam as duas à normalização e seriam
          percorridas duas vezes.

        `None` quer dizer que o escopo não é um diretório neste dispositivo,
        o que o chamador informa como escopo inválido. Um dispositivo que não
        está montado responde `None` para todo escopo; os chamadores
        verificam a conexão antes, para que o usuário ouça qual disco plugar,
        e não que a pasta dele está errada.
        """

    @abstractmethod
    def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
        """Devolve as pastas direto dentro de `scope`, ou `None` se ele não existe.

        **Um nível, e nenhum arquivo.** O chamador está desenhando um passo
        de uma árvore que o usuário está percorrendo, então o custo segue o
        que ele abriu, e não o tamanho do disco (RFC-031 §8.1). Arquivos nem
        são devolvidos: o que isto alimenta é um seletor de pastas cuja saída
        volta como `scopes[]`, e um arquivo não é um escopo.

        **Nada conta nada aqui.** Quantas imagens uma pasta tem indexadas é
        um fato da nossa própria tabela, respondido por `ImageRepository`;
        quantos arquivos há no disco abaixo dela é o percurso da subárvore
        que o RFC-029 §7.2 mediu como o custo dominante num disco mecânico a
        frio, e que o RFC-031 §2.3 se recusa a pagar duas vezes.

        `None` tem exatamente o sentido que tem em `resolve_scope()`: isto
        não é uma pasta neste dispositivo -- ausente, um arquivo, ou fora do
        dispositivo depois de resolvidas as junções NTFS. Um dispositivo que
        não está montado responde `None` para tudo, e os chamadores
        verificam a conexão antes, para que o usuário ouça qual disco
        plugar.

        A ordem não faz parte do contrato, porque a da plataforma também não
        faz: `scandir` devolve as entradas na ordem em que o sistema de
        arquivos as guarda. Um chamador que desenha a lista a ordena.
        """
