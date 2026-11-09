r"""Pontos de entrada HTTP para dispositivos, volumes e as suas pastas (RFC-031).

Seis rotas -- a sexta, a remoção das fotos que um scan não achou mais, é do
RFC-046 -- e o que elas têm em comum é o que se recusam a receber: **nenhuma
rota deste módulo aceita um caminho de arquivo, num corpo, numa query string,
num header ou num segmento de URL.** A propriedade que o RFC-030 §5.1
estabeleceu para o `/reveal` sobrevive a uma rota cujo propósito inteiro é
deixar o usuário acrescentar um disco, porque a direção é invertida -- o
servidor enumera os volumes e cunha uma identidade opaca para cada um, e o
cliente devolve uma. Um cliente nunca compõe uma localização, e uma identidade
que ele inventou não resolve para nada, que é justamente o que um caminho
inventado por ele não faria (RFC-031 §5.1).

`?path=` na listagem de pastas é a exceção aparente, e não é uma. É um **escopo
relativo ao dispositivo**, recusado pelo `JobScope` antes de qualquer coisa ser
aberta -- nada de `..`, de âncora de unidade, de compartilhamento UNC -- e
depois resolvido pelo locator, que confirma que o resultado ainda está dentro do
dispositivo depois de seguidas as junctions do NTFS. É a mesma validação que o
`POST /api/v1/jobs` aplica, e tem de ser: o que esta rota retorna é literalmente
o que o cliente devolve como `scopes[]`.

`def` síncrono, não `async def`, pela razão que o RFC-026 §9 dá e o `jobs.py`
documenta: tudo abaixo desta linha é uma chamada DBAPI bloqueante ou uma leitura
bloqueante do sistema de arquivos, e um `async def` a rodaria no event loop e
travaria toda requisição concorrente, `/health` inclusive.

Nenhum `try/except` em lugar nenhum. Um dispositivo que não existe, um disco na
gaveta, um tipo de volume desconhecido e uma pasta que não está lá são erros de
domínio, e o `error_handlers.py` transforma cada um em 404, 409, 400 e 400 sem
que nenhuma função aqui conheça esses números. O RFC-031 não acrescenta nenhuma
base de erro nova.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.list_device_folders import ListDeviceFoldersUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.list_mounted_volumes import ListMountedVolumesUseCase
from app.application.use_cases.purge_missing_images import PurgeMissingImagesUseCase
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.application.use_cases.rename_device import RenameDeviceUseCase
from app.domain.value_objects.device_id import DeviceId
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import (
    get_describe_devices_use_case,
    get_list_device_folders_use_case,
    get_list_devices_use_case,
    get_list_mounted_volumes_use_case,
    get_purge_missing_images_use_case,
    get_register_device_use_case,
    get_rename_device_use_case,
)
from app.presentation.schemas.device_schema import (
    DeviceDetailSchema,
    DeviceListSchema,
    FolderListSchema,
    PurgedImagesSchema,
    RegisterDeviceRequestSchema,
    RenameDeviceRequestSchema,
    VolumeListSchema,
)

router = APIRouter(tags=["devices"])
logger = get_logger(__name__)


@router.get(
    "/devices",
    status_code=status.HTTP_200_OK,
    response_model=DeviceListSchema,
    summary="List every known device, with connection resolved right now",
)
def list_devices(
    use_case: ListDevicesUseCase = Depends(get_list_devices_use_case),
) -> DeviceListSchema:
    """Retorna todo disco que o sistema conhece, conectado ou na gaveta.

    `connected` e `mount_point` descrevem **este instante** e são resolvidos
    por requisição a partir de uma única enumeração dos volumes montados,
    quantos dispositivos houver. Esse limite é o desenho (RFC-031 §4.1): a
    implementação óbvia pergunta ao locator uma vez por dispositivo, o que é
    certo para uma página de resultados de busca e aqui são N enumerações do
    mesmo sistema de arquivos.

    `connected: false` é uma resposta normal, não uma falha. A linha existe,
    toda foto daquele disco continua buscável, e as suas thumbnails continuam
    servidas (RFC-030 §4.1) -- o que falta ao usuário são os bytes.

    Nada de `percent_indexed`. A resposta traz `indexed_images`,
    `last_scan_file_count` e `last_scan_at` separados, para que ninguém consiga
    desenhar a razão sem ter a data do scan em mãos (ARCHITECTURE.md §15,
    RFC-031 §4.2).
    """
    return DeviceListSchema.from_summaries(use_case.execute())


@router.get(
    "/volumes",
    status_code=status.HTTP_200_OK,
    response_model=VolumeListSchema,
    summary="List the volumes attached to this machine right now",
)
def list_volumes(
    use_case: ListMountedVolumesUseCase = Depends(get_list_mounted_volumes_use_case),
) -> VolumeListSchema:
    """Responde *o que está plugado, e o que disso já conhecemos?*

    A pergunta que precede o cadastro de um disco, e a razão de o cadastro
    abaixo nunca precisar de um caminho: isto entrega ao cliente uma
    identidade opaca para cada volume, e o cliente devolve uma.

    **Deliberadamente a rota cara das cinco.** Cada volume custa uma chamada
    extra à plataforma pelo rótulo e pela capacidade, e ler uma capacidade pode
    fazer girar um disco externo que tinha estacionado as cabeças. É por isso
    que o `VolumeCatalog` tem dois métodos e que o `GET /devices` acima usa o
    barato -- juntar o detalhe à enumeração poria esse custo na frente de toda
    busca do produto (RFC-031 §4.1).

    Volumes que a plataforma se recusa a descrever são pulados, não informados
    como erros: um drive óptico vazio não é razão para parar de listar os discos
    que responderam (RFC-027 §7).
    """
    return VolumeListSchema.from_cataloged(use_case.execute())


@router.post(
    "/devices",
    status_code=status.HTTP_201_CREATED,
    response_model=DeviceDetailSchema,
    summary="Register a mounted volume as a device, by identity",
    responses={
        200: {
            "description": (
                "This volume was already registered. Nothing was created; "
                "the label sent, if any, was applied."
            )
        },
        400: {"description": "The volume identity is empty or of an unknown kind"},
        409: {"description": "No volume with that identity is mounted right now"},
    },
)
def register_device(
    request: RegisterDeviceRequestSchema,
    response: Response,
    use_case: RegisterDeviceUseCase = Depends(get_register_device_use_case),
    describer: DescribeDevicesUseCase = Depends(get_describe_devices_use_case),
) -> DeviceDetailSchema:
    r"""Cadastra o volume com esta identidade, ou retorna o dispositivo que ele já é.

    **201 na primeira vez, 200 na segunda, e a diferença importa.** `DeviceId`
    é `uuid5` sobre a identidade do volume, então cadastrar um disco conhecido
    cai na linha existente: nada foi criado, e um cliente que contasse criações
    contaria errado. Também não é um 409 -- um conflito descreve um pedido que o
    estado atual recusa, e este não é recusado, já está satisfeito
    (RFC-031 §6.1). O caso de uso informa o que aconteceu; esta função só
    escolhe o número.

    Um `label` enviado num segundo cadastro **é** aplicado. `save()` é um
    upsert por desenho, e a alternativa -- ignorá-lo em silêncio -- deixaria um
    usuário renomeando um disco sem efeito.

    **Nem `response` nem `describer` são entrada do cliente.** O FastAPI
    injeta o primeiro para que o status possa baixar para 200 e resolve o
    segundo como dependência; nada que um cliente envia alcança nenhum dos
    dois. O teste de assinatura afirma que a lista de parâmetros é exatamente
    esses quatro nomes, então um parâmetro de caminho acrescentado depois não
    tem onde se esconder.

    O `describer` é composto aqui, e não dentro do cadastro, pela razão por que
    a rota de busca compõe `SearchImagesUseCase` com
    `ResolveImageLocationUseCase`: resolver onde um disco está montado e quanto
    dele está indexado não é cadastrá-lo, e o caminho da CLI por
    `RegisterDeviceUseCase.register()` tem de ficar livre dos dois. É o que faz
    esta resposta dizer `connected: true` com um ponto de montagem real, em vez
    de informar como ausente um disco que o servidor acabou de achar montado.

    Uma identidade que ninguém detém é um 409 dizendo o que fazer a respeito --
    plugar o disco -- e **nada é escrito**. Uma identidade inventada não é uma
    localização: é procurada num mapeamento que o servidor produziu segundos
    atrás, e o que não está nele não existe. Um caminho, ao contrário, *é* uma
    localização, quer alguém tenha querido isso ou não, que é todo o RFC-031
    §5.1.
    """
    logger.info("device registration requested: kind=%s", request.volume_kind)
    registered = use_case.execute(
        volume_identity=request.volume_identity,
        volume_kind=request.volume_kind,
        label=request.label,
    )
    if not registered.created:
        response.status_code = status.HTTP_200_OK
    logger.info(
        "device %s registered (created=%s)", registered.device.id, registered.created
    )
    return DeviceDetailSchema.from_summary(describer.execute_one(registered.device))


@router.patch(
    "/devices/{device_id}",
    status_code=status.HTTP_200_OK,
    response_model=DeviceDetailSchema,
    summary="Rename a device",
    responses={404: {"description": "No device has this id"}},
)
def rename_device(
    device_id: UUID,
    request: RenameDeviceRequestSchema,
    use_case: RenameDeviceUseCase = Depends(get_rename_device_use_case),
) -> DeviceDetailSchema:
    """Muda o rótulo do disco. Nada mais de um dispositivo é editável.

    **Funciona com o disco na gaveta**, e essa é toda a razão de o renomear
    não exigir bytes: um nome é um fato da nossa tabela, e o RFC-027 §2.3 é
    explícito em que um disco desplugado é um dispositivo que o sistema
    conhece perfeitamente. A resposta ainda *informa* se o disco está aqui, e
    informar `false` é um desfecho tão bem-sucedido quanto informar `true`.

    Um corpo que traga qualquer coisa além de `label` é um **422**, não um
    campo ignorado em silêncio. `last_scan_file_count` é a coisa plausível de
    enviar e exatamente a que não pode ser gravável: é o denominador declarado
    do "% indexado", então um editável é um número inventado com aparência de
    medição (RFC-031 §7).

    A resposta tem o formato que o `GET /devices` retorna, resolvido do mesmo
    jeito, para que um cliente possa colocá-la direto na linha que renomeou em
    vez de reler a lista para saber se mais alguma coisa do disco ainda vale.
    """
    logger.info("rename requested for device %s", device_id)
    return DeviceDetailSchema.from_summary(
        use_case.execute(DeviceId(device_id), request.label)
    )


@router.get(
    "/devices/{device_id}/folders",
    status_code=status.HTTP_200_OK,
    response_model=FolderListSchema,
    summary="List one level of a device's folders, with each folder's state",
    responses={
        400: {"description": "The path escapes the device, or is not a folder on it"},
        404: {"description": "No device has this id"},
        409: {"description": "The device is not connected"},
    },
)
def list_device_folders(
    device_id: UUID,
    path: str = Query(
        default="",
        description=(
            "Folder to list, relative to the device root, e.g. '2018'. "
            "Omit, or send an empty string, for the root. A drive letter, "
            "a leading separator, a UNC share or any '..' is refused "
            "before anything is opened."
        ),
    ),
    use_case: ListDeviceFoldersUseCase = Depends(get_list_device_folders_use_case),
) -> FolderListSchema:
    """Retorna as pastas logo dentro de `path`, cada uma com o seu estado de indexação.

    **Um nível por requisição.** Não um primeiro nível achatado, que
    transformaria "indexar o casamento" em "indexar todo 2018" -- cinco horas
    de inferência para chegar a trezentas fotos -- nem a árvore inteira, que
    custa o tamanho do disco toda vez que a tela abre (RFC-031 §8.1).

    **Os caminhos que voltam têm a grafia do sistema de arquivos, não a que
    foi pedida.** Uma requisição de `?path=FOTOS/2018` contra uma pasta chamada
    `Fotos/2018` responde `"path": "Fotos/2018"`, porque esse é o texto que o
    cliente vai enviar em `scopes[]`, e o `POST /jobs` compara as partes do
    caminho exatamente.

    **Exige o disco plugado: 409, dizendo qual.** Ao contrário do renomear
    acima, este precisa de bytes, e *qual disco plugar* é mais útil do que
    ouvir que uma pasta não está lá.

    Nenhuma contagem de arquivos no disco e nenhuma porcentagem por pasta. Não
    há denominador, e obter um significa percorrer a subárvore -- a leitura que
    o RFC-029 §7.2 mediu como dominante num disco mecânico frio.
    `indexed_images` é exato e é o que desenhar (RFC-031 §2.3).

    Sem paginação. Uma pasta com milhares de subpastas retorna todas; a dívida
    está declarada no RFC-031 §8.3 e será medida antes de ser paga.
    """
    return FolderListSchema.from_domain(use_case.execute(DeviceId(device_id), path))


@router.delete(
    "/devices/{device_id}/missing-images",
    status_code=status.HTTP_200_OK,
    response_model=PurgedImagesSchema,
    summary="Remove from the index the photos a scan no longer found on a disk",
    responses={
        404: {"description": "No device has this id"},
        409: {"description": "The device has a pending or running indexing job"},
    },
)
def purge_missing_images(
    device_id: UUID,
    use_case: PurgeMissingImagesUseCase = Depends(get_purge_missing_images_use_case),
) -> PurgedImagesSchema:
    """Apaga as fotos deste disco que um scan marcou como não achadas (RFC-046).

    **A única rota que apaga linhas do índice**, e só estas: as que um scan do
    disco inteiro não achou, e que estão escondidas de toda leitura desde
    então. As linhas saem numa transação, e só depois as suas thumbnails, do
    cache do próprio aplicativo. **Irreversível**, e é por isso que a interface
    pergunta antes, com o número.

    **409 enquanto o disco tem um job**, pendente ou rodando: o job termina
    desmarcando as fotos que achou de novo, e isto as apagaria antes. O pedido
    é legítimo e o estado não o permite -- o 409 do RFC-030.

    **O disco não precisa estar conectado.** As marcas vieram de um scan que foi
    verificado quando rodou, e esquecer o que não está mais num disco não
    precisa de nada do disco. Nada no sistema de arquivos do usuário é tocado,
    então nenhuma das guardas do RFC-030 se aplica.
    """
    logger.info("purge of missing images requested for device %s", device_id)
    purged = use_case.execute(DeviceId(device_id))
    logger.info("device %s: %d missing images purged", device_id, purged)
    return PurgedImagesSchema(purged=purged)
