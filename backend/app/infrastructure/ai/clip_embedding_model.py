"""Adaptador de embedding CLIP de produção (RFC-023).

Implementa `EmbeddingModelPort` com `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`,
o checkpoint que o bake-off do RFC-023 escolheu. Todo detalhe de CLIP, torch
e transformers fica confinado a este pacote: Domain e Application só veem
`EmbeddingModelPort`, `Image` e `EmbeddingVector`, então um RFC futuro pode
trocar por um CLIP com fine-tuning, pelo SigLIP ou por um backend ONNX sem
mexer em nenhum contrato acima da Infrastructure.

Desde o RFC-035, o checkpoint é carregado de um diretório, e nunca pelo nome:
o snapshot que o lockfile fixa, materializado em `runtime\\models` e
verificado por `model_runtime` antes da primeira carga. Qual modelo é este
quem decide é o lockfile, e portanto a versão do código.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import torch
from PIL import Image as PILImage
from transformers import AutoProcessor, CLIPModel
from transformers.processing_utils import ProcessorMixin

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.ai.device import resolve_device
from app.infrastructure.ai.model_runtime import ModelDirectory, load_failed
from app.infrastructure.ai.query_adapter import QueryAdapter
from app.infrastructure.ai.query_translator import QueryTranslator
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

# Fixado pelo bake-off, não é um parâmetro ajustável. `a photo of {query}` fez
# 60,0% strict-EN / 80,4% pairwise-EN no checkpoint escolhido, contra 56,0% /
# 78,8% da consulta pura e 52,0% / 73,5% de `an aerial photo of {query}`
# (`clip_template_run_output.log`). Embora este seja um produto de imagens
# aéreas, o template específico para fotos aéreas foi o pior dos três.
TEXT_PROMPT_TEMPLATE = "a photo of {query}"

IMAGE_PROCESSOR_BACKEND = "torchvision"
"""O processor de imagem, pedido pelo nome (RFC-041 §5.5).

Sem nome, o `transformers` 5.15 escolhe o `torchvision` quando o pacote está
instalado e o do PIL quando não está, e os dois redimensionam diferente: em
dez imagens de demo, os pixels deles diferem em até 0,015 (RFC-041 §14). Todo
vetor do projeto veio do `torchvision`, que o app nunca importa, então uma
árvore sem ele codificaria outros vetores sem dizer nada. Pedido pelo nome, e
conferido depois da carga: o `transformers` cai para o PIL mesmo assim.
"""


DRAFT_TARGET_EDGE = 1000
"""Fixado pela medição do RFC-045, não é ajustável: outro valor, outros vetores.

Pedido ao decodificador JPEG antes da decodificação (`draft("RGB", (1000,
1000))`): o Pillow escolhe então a maior redução DCT -- 1/2, 1/4 ou 1/8 -- que
deixa os dois lados com pelo menos este tamanho, e nenhuma quando o lado menor
tem menos que o dobro dele. Nas fotos de drone de 12 MP isso dá 1/2, o que
levou o pré-processamento de 163 para 94 ms; fotos que não são reduzidas -- as
pequenas, todo PNG e TIFF, o corpus de demo inteiro -- dão exatamente o vetor
que davam antes (RFC-045 §2.2). Fora de `Settings`, pelo mesmo motivo do
template de texto e do backend do processor: mudá-lo muda todo vetor, e o
índice recomeça do zero quando os vetores mudam (§5.9).
"""


def image_processor_backend(processor: object) -> str | None:
    """`torchvision` ou `pil`: por qual backend passam as imagens do processor.

    Lido da ancestralidade da classe, pelo nome, para que nada aqui importe as
    classes de backend, uma das quais precisa de `torchvision`.
    """
    image_processor = getattr(processor, "image_processor", None)
    ancestry = {cls.__name__ for cls in type(image_processor).__mro__}
    if "TorchvisionBackend" in ancestry:
        return "torchvision"
    if "PilBackend" in ancestry:
        return "pil"
    return None


class ClipEmbeddingModel(EmbeddingModelPort):
    """Codifica imagens e texto num mesmo espaço de similaridade CLIP de 512 dims.

    Os dois modelos são carregados sob demanda no primeiro uso e reaproveitados
    pela vida da instância, então construir o adaptador -- o que a ligação das
    dependências faz quase na hora do import -- não custa nada e nunca toca o
    disco (RFC-023 §10). `model_directory` é chamado nessa primeira carga, e é
    onde os arquivos são conferidos com o lockfile (RFC-035 §6); o tradutor
    recebe o seu, para a sua própria primeira carga.

    Erros do Pillow e da inferência do modelo propagam de propósito.
    `IndexingWorker.run()` já isola as falhas por arquivo, então um segundo
    `except` genérico aqui só esconderia qual arquivo falhou e por quê. A
    única exceção é a própria carga, que vira um `ModelUnavailableError`: uma
    linha para o usuário, e o traceback para o log.

    `query_adapter`, quando dado, é a matriz do nível A do RFC-042. Também é
    chamado na primeira carga, e só `encode_text()` aplica o que ele devolve.
    Sem ele, o vetor de uma consulta é o do próprio CLIP, que é contra o que
    os experimentos e os vetores de referência do RFC-035 comparam.
    """

    def __init__(
        self,
        model_directory: ModelDirectory,
        translator: QueryTranslator,
        expected_dimension: int | None = None,
        device: torch.device | None = None,
        query_adapter: Callable[[], QueryAdapter] | None = None,
    ) -> None:
        self._model_directory = model_directory
        self._expected_dimension = expected_dimension or settings.embedding_dimension
        self._device = device if device is not None else resolve_device()
        self._translator = translator
        self._query_adapter_source = query_adapter
        self._query_adapter: QueryAdapter | None = None
        self._loaded_from: Path | None = None
        self._model: CLIPModel | None = None
        self._processor: ProcessorMixin | None = None
        self._load_lock = threading.Lock()
        """Uma carga por vez; ver `_ensure_loaded()`."""

    def encode_image(
        self, image: Image, content: bytes | None = None
    ) -> EmbeddingVector:
        """Decodifica os pixels da imagem e devolve o embedding CLIP deles.

        De `content` quando quem chama já leu o arquivo (RFC-045 §5.3), do
        arquivo nos outros casos; o vetor é o mesmo.
        """
        model, processor = self._ensure_loaded()
        pixel_values = self._preprocess(processor, image, content)

        with torch.no_grad():
            features = model.get_image_features(
                pixel_values=pixel_values.to(self._device)
            )

        return self._to_embedding_vectors(features.pooler_output, expected_rows=1)[0]

    def encode_images(
        self,
        images: Sequence[Image],
        contents: Sequence[bytes | None] | None = None,
    ) -> list[EmbeddingVector]:
        """Codifica várias imagens numa só passada pelo modelo (RFC-024 §5).

        Sobrescreve o default por imagem da porta porque o CLIP de fato tem
        um jeito mais rápido de fazer isso: medido no corpus de demo em CPU,
        um lote de 8 roda a 1,40x a vazão de um lote de 1, com os embeddings
        iguais dentro da precisão de ponto flutuante.

        O pré-processamento é um laço de propósito, e não uma única chamada
        `processor(images=[...])`. Cada iteração decodifica um arquivo em
        tamanho cheio, reduz para a entrada fixa do modelo e descarta a cópia
        em tamanho cheio antes de abrir o arquivo seguinte, então o que se
        acumula no lote são N tensores pequenos, e não N fotos decodificadas.
        Essa diferença não aparece no corpus de demo de 1024x768 (~1,8 MB
        cada) e decide se o pipeline sobrevive a uma pasta de fotos de 24
        megapixels, em que cada original decodificado teria ~72 MB (RFC-024
        §9).

        Erros propagam, exatamente como em `encode_image()`. Um arquivo
        ilegível, portanto, faz o lote inteiro falhar, e isso é intencional:
        quem chama tenta o lote de novo uma imagem por vez para descobrir qual
        arquivo foi, e a decisão do RFC-023 §4 de que este adaptador não
        engole nada continua valendo.

        `contents` junta cada imagem aos seus bytes, ou a `None` para ler o
        arquivo (RFC-045 §5.3), como a porta descreve.
        """
        if contents is not None and len(contents) != len(images):
            raise ValueError(
                f"{len(contents)} contents were given for {len(images)} images"
            )
        if not images:
            return []

        sources = contents if contents is not None else [None] * len(images)
        model, processor = self._ensure_loaded()
        pixel_values = torch.cat(
            [
                self._preprocess(processor, image, content)
                for image, content in zip(images, sources, strict=True)
            ],
            dim=0,
        )

        with torch.no_grad():
            features = model.get_image_features(
                pixel_values=pixel_values.to(self._device)
            )

        return self._to_embedding_vectors(features.pooler_output, len(images))

    def _preprocess(
        self,
        processor: ProcessorMixin,
        image: Image,
        content: bytes | None = None,
    ) -> torch.Tensor:
        """Reduz um arquivo do disco ao tensor de tamanho fixo que o modelo consome.

        O CLIP foi treinado em RGB de 3 canais. Converter aqui, explicitamente,
        em vez de depender dos internos do processor, põe os casos difíceis do
        RFC-022 6.2 -- tons de cinza, CMYK, TIFF de 16 bits, RGBA -- num único
        caminho documentado. `convert()` também força a decodificação que o
        `open()` preguiçoso do Pillow adia, então um arquivo de zero bytes ou
        truncado falha aqui, e não em algum ponto mais fundo do modelo.

        A imagem decodificada em tamanho cheio é uma variável local deste
        método e de mais nada, então fica inalcançável no instante em que o
        método retorna. Essa é toda a estratégia de memória: só o tensor
        devolvido -- fixo na resolução de entrada do modelo, seja qual for a
        foto de origem -- pode sobreviver a um arquivo.

        **Os bytes vêm de `content` quando há** (RFC-045 §5.3): a leitura
        antecipada leu o arquivo para o hash, e decodificar esses bytes dá o
        mesmo tensor que decodificar o arquivo.

        **Ao decodificador se pede antes um rascunho reduzido**
        (`DRAFT_TARGET_EDGE`, RFC-045 §5.6), sem condição em volta da
        chamada: ela não faz nada em formato nenhum além de JPEG, nem num JPEG
        cujo lado menor tenha menos de 2.000 px. Um MPO -- como o Pillow abre
        uma foto DJI -- é um `JpegImageFile`, e o rascunho vale para o
        primeiro quadro dele, o que é lido aqui. `POST /images/search/by-image`
        também passa por este método, então uma foto de consulta é reduzida
        pela mesma regra que as fotos do índice. O pedido tem de vir antes de
        `convert()`, que é quem decodifica.
        """
        source = (
            io.BytesIO(content)
            if content is not None
            else image.require_absolute_path().value
        )
        with PILImage.open(source) as opened:
            opened.draft("RGB", (DRAFT_TARGET_EDGE, DRAFT_TARGET_EDGE))
            pixels = opened.convert("RGB")
            inputs: Mapping[str, torch.Tensor] = processor(
                images=pixels, return_tensors="pt"
            )

        return inputs["pixel_values"]

    def encode_text(self, text: str) -> EmbeddingVector:
        """Codifica uma consulta no mesmo espaço em que `encode_image` escreve.

        O CLIP lê no máximo 77 tokens. Um prompt mais longo -- uma consulta
        longa, ou uma tradução em que o Marian se repete -- é cortado nesse
        tamanho, em vez de fazer a busca falhar.

        Com um adaptador de consulta, o vetor normalizado passa por ele por
        último (RFC-042 nível A). Os vetores das fotos nunca são adaptados.
        """
        model, processor = self._ensure_loaded()

        inputs = processor(
            text=[self.build_prompt(text)],
            return_tensors="pt",
            padding=True,
            truncation=True,
        )

        with torch.no_grad():
            features = model.get_text_features(**self._to_device(inputs))

        return self._to_embedding_vectors(
            features.pooler_output, expected_rows=1, adapter=self._query_adapter
        )[0]

    def build_prompt(self, text: str) -> str:
        """Devolve a string exata, em inglês, entregue ao encoder de texto do CLIP.

        O pipeline inteiro da consulta numa linha: detecta o idioma, traduz
        português para inglês e aplica o template. Público para que o prompt
        e a tradução possam ser verificados sem carregar o próprio CLIP.
        """
        return TEXT_PROMPT_TEMPLATE.format(query=self._translator.to_english(text))

    def warm_up(self) -> None:
        """Carrega o checkpoint CLIP agora, sem tocar no tradutor.

        `encode_text()` também o carregaria, e arrastaria junto o modelo de
        tradução (`build_prompt` detecta o idioma antes) -- 600 MB que o
        executor de jobs nunca vai usar, porque um worker codifica imagens e
        nunca consultas. Então isto carrega só a torre de que precisa.
        """
        self._ensure_loaded()

    def _ensure_loaded(self) -> tuple[CLIPModel, ProcessorMixin]:
        """Carrega o checkpoint no primeiro uso e o reaproveita nas chamadas seguintes.

        O diretório vem primeiro, conferido: nada é desserializado antes de
        cada arquivo ser o que o lockfile nomeia (RFC-035 §6.2).
        `local_files_only` é escrito na chamada, em vez de ficar a cargo do
        modo offline, para valer mesmo se a variável chegou tarde demais
        (§5.1): um arquivo opcional que o diretório não tem fica simplesmente
        ausente, e nunca é procurado no Hub.

        **Uma vez, sob um lock, com a verificação repetida dentro dele**
        (RFC-036 §5.3). Duas primeiras requisições que achem o modelo
        descarregado -- a corrida do RFC-026 §9.1, que um warm-up que
        bloqueava a porta mantinha longe -- carregariam, as duas, o
        checkpoint. Como o warm-up roda numa thread ao lado da porta aberta,
        uma busca que chega durante ele espera a carga em andamento em vez de
        começar uma segunda. Depois de carregado, a verificação fora do lock
        é tudo o que uma chamada paga.
        """
        if self._model is None or self._processor is None:
            with self._load_lock:
                if self._model is None or self._processor is None:
                    self._model, self._processor = self._load()
        return self._model, self._processor

    def _load(self) -> tuple[CLIPModel, ProcessorMixin]:
        directory = self._model_directory()
        logger.info("Loading CLIP model from %s onto %s", directory, self._device)
        try:
            model = CLIPModel.from_pretrained(directory, local_files_only=True)
            # Três lacunas de tipagem do transformers 5.15, não erros de tipo
            # de verdade: `nn.Module.eval` não tem anotação, `PreTrainedModel.to`
            # está embrulhado num decorator cuja assinatura o mypy lê como se
            # recebesse o próprio modelo, e `AutoProcessor.from_pretrained` não
            # tem anotação. `warn_unused_ignores` vai apontar cada um no momento
            # em que uma versão os tipar direito.
            model.eval()  # type: ignore[no-untyped-call]
            loaded = model.to(self._device)  # type: ignore[arg-type]
            processor = AutoProcessor.from_pretrained(  # type: ignore[no-untyped-call]
                directory, local_files_only=True, backend=IMAGE_PROCESSOR_BACKEND
            )
            backend = image_processor_backend(processor)
            if backend != IMAGE_PROCESSOR_BACKEND:
                raise RuntimeError(
                    f"The image processor's backend is {backend}, not "
                    f"{IMAGE_PROCESSOR_BACKEND}: is the torchvision package missing? "
                    "Its vectors would not be the index's (RFC-041 section 5.5)."
                )
            if self._query_adapter_source is not None:
                adapter = self._query_adapter_source()
                if adapter.dimension != self._expected_dimension:
                    raise RuntimeError(
                        f"The query adapter is {adapter.dimension}-dimensional,"
                        f" and the vectors are {self._expected_dimension}"
                    )
                self._query_adapter = adapter
        except Exception as error:
            raise load_failed(directory) from error
        self._loaded_from = directory
        return loaded, processor

    def _to_device(self, inputs: Mapping[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        return {key: value.to(self._device) for key, value in inputs.items()}

    def _to_embedding_vectors(
        self,
        features: torch.Tensor,
        expected_rows: int,
        adapter: QueryAdapter | None = None,
    ) -> list[EmbeddingVector]:
        """Valida, normaliza em L2 e eleva as features do CLIP ao tipo do domínio.

        Recebe `expected_rows` em vez de supor uma, para que um lote de N e
        um lote de 1 sejam validados e normalizados pelo mesmo código.
        `normalize(..., dim=-1)` é por linha, então o lote não muda nenhum
        embedding individual -- e é isso que faz da exigência de equivalência
        do RFC-024 uma propriedade da aritmética, e não uma esperança.

        Dois fatos específicos da versão do transformers guiam este método,
        os dois conferidos no transformers 5.15 instalado, e não copiados de
        um exemplo:

        1. `get_image_features()` / `get_text_features()` NÃO devolvem um
           tensor. Devolvem um `BaseModelOutputWithPooling` cujo
           `.pooler_output` o método sobrescreveu com o embedding projetado.
           `.last_hidden_state` nesse mesmo objeto é o estado do encoder de
           768 dims antes da projeção -- o espaço errado, e, em silêncio, o
           tamanho errado.
        2. Nenhuma das saídas vem normalizada: as normas L2 medidas são ~11,6
           para imagens e ~8,5 para texto. Similaridade de cosseno é a
           métrica para a qual o índice HNSW é construído
           (`vector_cosine_ops`), então normalizar é obrigatório, e o lugar é
           aqui, e não em `EmbeddingVector` -- esse value object não depende
           de modelo e não pode impor a convenção de um espaço de embedding a
           todo modelo futuro (RFC-023 6).
        """
        if features.ndim != 2 or features.shape[0] != expected_rows:
            wanted = (
                "a single (1, N) embedding"
                if expected_rows == 1
                else f"{expected_rows} ({expected_rows}, N) embeddings"
            )
            raise ValueError(
                f"The model in {self._loaded_from} returned features of shape "
                f"{tuple(features.shape)}; expected {wanted}"
            )

        dimension = features.shape[1]
        if dimension != self._expected_dimension:
            raise ValueError(
                f"The model in {self._loaded_from} produced {dimension} "
                f"dimensions, but settings.embedding_dimension is "
                f"{self._expected_dimension}"
            )

        normalized = torch.nn.functional.normalize(features, p=2.0, dim=-1)
        if adapter is not None:
            normalized = adapter.apply(normalized)
        return [EmbeddingVector(row) for row in normalized.detach().cpu().tolist()]
