"""
RUN THESE BEFORE RUNNING THE CODE:
    - in CMD/SHELL:
        pip install nltk corpy sentencepiece transformers
    - in PYTHON:
        import nltk
        nltk.download('punkt')
        nltk.download('averaged_perceptron_tagger')
        nltk.download('universal_tagset')
"""
import corpy.morphodita
import nltk
import transformers
import torch
import numpy as np
import os
import logging

from typing import List, Dict, Set, Tuple
from math import floor
from collections import deque
import random
import re

DIRECT_CITATION_REGEX = re.compile(r'"[^"]{30,}"')
CITATION_SPAN_REGEX = re.compile(
    r'\[(?:\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\]'
    r'|\((?:[^()]{0,80}?\b(?:19|20)\d{2}[a-z]?(?:\s*[,;]\s*[^()]*)?)\)',
    re.IGNORECASE,
)
REFERENCES_HEADING_REGEX = re.compile(
    r"^(?:references|bibliography|works cited|literature cited|reference list|"
    r"zdroje|literatura|seznam literatury|referencie|zoznam použitých zdrojov)$",
    re.IGNORECASE,
)
BIBLIOGRAPHIC_IDENTIFIER_REGEX = re.compile(
    r"\b(?:doi|isbn|issn|urn|arxiv)\b|https?://|www\.|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
CAPTION_REGEX = re.compile(
    r"^\s*(?:figure|fig\.?|table|graph|chart|listing|code|source|"
    r"obrázek|obr\.?|tabulka|graf|kód|ukázka kódu|zdroj|"
    r"obrázok|tabuľka)\b",
    re.IGNORECASE,
)
TOC_REGEX = re.compile(r"(?:\.\s*){3,}\d+\s*$")
CITATION_ONLY_REGEX = re.compile(
    r"^\s*(?:\[(?:\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\]|"
    r"\([^()]{0,80}\b(?:19|20)\d{2}[a-z]?[^()]{0,80}\))\s*[.,;]?\s*$",
    re.IGNORECASE,
)
LIST_MARKER_REGEX = re.compile(r"^\s*(?:[-*•]|(?:\d+|[A-Za-z])[.)])\s+")
BOILERPLATE_REGEX = re.compile(
    r"\b(?:"
    r"hereby\s+declare|declaration|čestné\s+prohlášení|"
    r"prohlašuji|vyhlasujem|"
    r"thank(?:s|ful)?|grateful|acknowledg\w*|"
    r"poděkov\w*|ďak\w*|poďakov\w*|"
    r"vedoucí\s+práce|vedúc[ia]\s+práce|supervis(?:or|ion)|"
    r"název\s+práce|názov\s+práce|name\s+of\s+author|"
    r"keywords?|klíčová\s+slova|kľúčové\s+slová|"
    r"abstract|abstrakt|anotace"
    r")\b"
    # Numbered figure / table references anywhere in the sentence:
    # matches "Figure 4", "Figure 4.1", "Fig. 12.3.2", "Table 2", "Tab. 3.1".
    # Fires on captions the CAPTION_REGEX missed AND on mid-sentence
    # references like "As shown in Figure 4.1, ..." -- rejecting the whole
    # sentence in the latter case is intentional: these sentences are
    # cross-references, and blanking a word inside them usually produces
    # a question about the reference, not about the content.
    r"|\b(?:figure|fig\.?|table|tab\.?)\s+\d+(?:\.\d+)*\b",
    re.IGNORECASE,
)

# Duplicate-candidate deferral (see get_plausible_words). Candidates are ordered
# most-frequent-first, so a very common word can appear hundreds of times in a
# row. Once DUP_DEFER_AFTER occurrences of the same word have been LM-verified
# without any being picked, the remaining occurrences are pushed
# DUP_DEFER_DISTANCE places further down the queue so other, rarer words get a
# turn before we spend inference on more copies of a likely filler word. In the
# worst case every candidate is still processed.
def _env_flag(name, default=True):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


DUP_DEFER_AFTER = int(os.getenv("AVER_DUP_DEFER_AFTER", "5"))
DUP_DEFER_DISTANCE = int(os.getenv("AVER_DUP_DEFER_DISTANCE", "100"))


def filter_settings():
    """Text-quality filter knobs. Read on each call so a sweep can vary them.

    Defaults match the production heuristics from the filter commit.
    """
    return {
        "references": _env_flag("AVER_FILTER_REFERENCES", True),
        "structural": _env_flag("AVER_FILTER_STRUCTURAL", True),
        "weak_text": _env_flag("AVER_FILTER_WEAK_TEXT", True),
        "citations": _env_flag("AVER_FILTER_CITATIONS", True),
        "boilerplate": _env_flag("AVER_FILTER_BOILERPLATE", True),
        "non_letter_ratio": _env_float("AVER_FILTER_NON_LETTER_RATIO", 0.40),
        "min_alpha_tokens": _env_int("AVER_FILTER_MIN_ALPHA_TOKENS", 8),
        "min_content_tokens": _env_int("AVER_FILTER_MIN_CONTENT_TOKENS", 3),
        "min_content_ratio": _env_float("AVER_FILTER_MIN_CONTENT_RATIO", 0.35),
        "weak_score": _env_int("AVER_FILTER_WEAK_SCORE", 1),
        # Reject a sentence that tokenises to more than this many model tokens.
        # The model tokenizer already truncates the LM input to 512, but a single
        # very long "sentence" (usually a PDF-extraction glitch: bibliography or
        # table row collapsed onto one line) still spawns hundreds of candidate
        # n-grams, each keeping the whole truncated sentence in memory during
        # beam search. Filtering those sentences upfront cuts the batch count
        # and stops the CPU deployment from tripping the OOM killer. Set to 0
        # to disable. Estimated by 4 characters per token when the analyzer
        # has no tokenizer loaded (test/unit-only path).
        "max_sentence_tokens": _env_int("AVER_FILTER_MAX_SENTENCE_TOKENS", 1024),
    }


translations_word_class = {}
translations_word_class['N'] = 'NOUN'
translations_word_class['V'] = 'VERB'
translations_word_class['J'] = 'CONJ'
translations_word_class['R'] = 'ADP'
translations_word_class['P'] = 'PRON'
translations_word_class['D'] = 'ADV'
translations_word_class['C'] = 'NUM'
translations_word_class['T'] = 'PRT'
translations_word_class['A'] = 'ADJ'

"""
This logit processor, when used together with the transformers generate function,
generates only sequences of the specified count of words
"""
class GenerateNWordsLogitsProcessor(transformers.LogitsProcessor):
    def __init__(self, num_of_words: int, eos_token_id: int,  suppress_tokens: Set[int]):
        self.num_of_words = num_of_words
        self.eos_token_id = eos_token_id
        self.suppress_tokens_set = set(suppress_tokens)
        self.suppress_tokens_list = list(suppress_tokens)

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
        for beam_idx in range(input_ids.shape[0]):
            num_of_words = len([item for item in input_ids[beam_idx] if item.item() in self.suppress_tokens_set])
            if num_of_words < self.num_of_words:
                scores[beam_idx, self.eos_token_id] = -float("inf")
            else:
                scores[beam_idx, self.suppress_tokens_list] = -float("inf")

        return scores


class AnalyzedWord():
    """Data class containing information about one blanked sentence"""
    def __init__(self, word, word_class, language, sentence, blanked_sentence, paragraph_index, sentence_index, token_index, ml_pos=-1, predictions=None, cloze_class=None):
        self.word = word
        self.word_class = word_class
        self.language = language
        self.sentence = sentence
        self.blanked_sentence = blanked_sentence
        self.paragraph_index = paragraph_index
        self.sentence_index = sentence_index
        self.token_index = token_index
        self.ml_pos = ml_pos
        self.predictions = predictions
        # Name of the cloze class (PICK_CLASSES key) that produced this candidate,
        # e.g. "unigrams_ADJ" or "trigrams_w_ADJ". Set in scan_document.
        self.cloze_class = cloze_class
        # True when this candidate was added by the last-resort random fill in
        # get_plausible_words (Step C) because no plausible candidate was
        # available. The worker maps this flag to a synthetic "random" API
        # category so the frontend can apply different thresholds to it.
        self.random_fallback = False


class DocumentAnalyzer:
    """
    Class provides methods that given a text (paragraph) provides a list of the most plausible words (blanks/blanked sentences).
    """
    def __init__(self, model_name="google/mt5-large"):
        logging.basicConfig(level=os.getenv("AVER_LOG_LEVEL", "INFO"))
        self.logger = logging.getLogger("aver.inference")
        self.picked_words = {}
        self.stopwords = {}

        # Load stopwords
        with open("./MLmethod/stopwords_tagger/slovak_stopwords.txt", "r", encoding="utf8") as f:
            self.stopwords['sk'] = f.read().splitlines() 
        with open("./MLmethod/stopwords_tagger/czech_stopwords.txt", "r", encoding="utf8") as f:
            self.stopwords['cs'] = f.read().splitlines() 
        with open("./MLmethod/stopwords_tagger/english_stopwords.txt", "r", encoding="utf8") as f:
            self.stopwords['en'] = f.read().splitlines()

        # Load taggers
        self.tagger_czech = corpy.morphodita.Tagger("./MLmethod/stopwords_tagger/czech-morfflex-pdt-161115-pos_only.tagger")
        self.tagger_slovak = corpy.morphodita.Tagger("./MLmethod/stopwords_tagger/slovak-morfflex-pdt-170914-pos_only.tagger")
        
        # Load language model
        self.inference_backend = os.getenv("AVER_INFERENCE_BACKEND", "torch").lower()
        self.use_openvino = self.inference_backend in ("openvino", "ov")
        self.logger.info("Inference backend: %s", self.inference_backend)
        if self.use_openvino:
            try:
                from optimum.intel import OVModelForSeq2SeqLM
            except ImportError as exc:
                raise RuntimeError("OpenVINO backend requested but optimum-intel is not installed.") from exc

            self.device = "cpu"
            ov_model_dir = os.getenv("AVER_OPENVINO_MODEL_DIR")
            export_ov = os.getenv("AVER_OPENVINO_EXPORT", "0").lower() in ("1", "true", "yes")
            self.logger.info("OpenVINO model dir: %s", ov_model_dir or "<not set>")
            self.logger.info("OpenVINO export enabled: %s", export_ov)
            if ov_model_dir:
                ov_model_dir = os.path.abspath(ov_model_dir)
                if not os.path.isdir(ov_model_dir):
                    raise RuntimeError(f"OpenVINO model directory not found: {ov_model_dir}")
                if export_ov:
                    export_ov = False
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                self.logger.info("Local OpenVINO mode: HF_HUB_OFFLINE=%s TRANSFORMERS_OFFLINE=%s",
                                 os.getenv("HF_HUB_OFFLINE"), os.getenv("TRANSFORMERS_OFFLINE"))
                self.logger.info("OpenVINO directory contents: %s", sorted(os.listdir(ov_model_dir)))
                required_files = [
                    "openvino_encoder_model.xml",
                    "openvino_encoder_model.bin",
                    "openvino_decoder_model.xml",
                    "openvino_decoder_model.bin",
                    "config.json",
                ]
                missing_files = [name for name in required_files if not os.path.exists(os.path.join(ov_model_dir, name))]
                if missing_files:
                    raise RuntimeError(f"OpenVINO model directory missing files: {missing_files}")
                self.model = OVModelForSeq2SeqLM.from_pretrained(ov_model_dir, local_files_only=True)
                self.tokenizer = transformers.AutoTokenizer.from_pretrained(ov_model_dir, local_files_only=True)
                self.logger.info("Loaded OpenVINO model from local directory.")
            else:
                self.model = OVModelForSeq2SeqLM.from_pretrained(model_name, export=export_ov)
                self.tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)
                self.logger.info("Loaded OpenVINO model from hub: %s", model_name)
        else:
            dtype_name = os.getenv("AVER_MODEL_DTYPE", "float32").lower()
            dtypes = {
                "float32": torch.float32,
                "fp32": torch.float32,
                "float16": torch.float16,
                "fp16": torch.float16,
                "bfloat16": torch.bfloat16,
                "bf16": torch.bfloat16,
            }
            if dtype_name not in dtypes:
                raise ValueError(
                    f"unsupported AVER_MODEL_DTYPE={dtype_name!r}; "
                    f"choose one of {sorted(dtypes)}")
            self.model_dtype = dtypes[dtype_name]
            quantization = os.getenv(
                "AVER_MODEL_QUANTIZATION", "none").strip().lower()
            self.quantized = quantization in ("4bit", "nf4")
            if quantization not in ("", "none", "4bit", "nf4"):
                raise ValueError(
                    f"unsupported AVER_MODEL_QUANTIZATION={quantization!r}; "
                    "choose none or 4bit")

            if self.quantized:
                if not torch.cuda.is_available():
                    raise RuntimeError(
                        "4-bit quantization requires an available CUDA GPU")
                quantization_config = transformers.BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_use_double_quant=True,
                    bnb_4bit_compute_dtype=self.model_dtype,
                )
                self.device = "cuda:0"
                self.logger.info(
                    "Torch device: %s; NF4 4-bit weights; compute dtype: %s",
                    self.device, self.model_dtype,
                )
                self.model = (
                    transformers.MT5ForConditionalGeneration.from_pretrained(
                        model_name,
                        quantization_config=quantization_config,
                        device_map={"": self.device},
                    )
                )
            else:
                # Keep an unquantized model on the CPU while the worker is idle.
                # The worker moves it to its selected GPU for each job.
                self.device = "cpu"
                self.logger.info(
                    "Torch device: %s; model dtype: %s "
                    "(GPU, if any, acquired per job)",
                    self.device, self.model_dtype,
                )
                self.model = (
                    transformers.MT5ForConditionalGeneration.from_pretrained(
                        model_name, torch_dtype=self.model_dtype
                    ).to(self.device)
                )
            self.model.eval()
            self.tokenizer = transformers.T5Tokenizer.from_pretrained(model_name)
        self.suppress_tokens = DocumentAnalyzer.get_suppress_tokens(self.tokenizer)

    def move_to_device(self, device: str) -> None:
        """Move the language model onto `device` (e.g. 'cuda:3' or 'cpu').

        No-op for the OpenVINO backend, whose model is not a torch module.
        """
        if getattr(self, "use_openvino", False):
            return
        if getattr(self, "quantized", False):
            if torch.device(device) != torch.device(self.device):
                raise RuntimeError(
                    f"the 4-bit model is fixed on {self.device}, not {device}")
            return
        if device == self.device:
            return
        self.logger.info("Moving model to device: %s", device)
        self.model = self.model.to(device)
        self.device = device

    def release_device(self) -> None:
        """Move the model back to CPU and free GPU memory for other jobs."""
        if getattr(self, "use_openvino", False):
            return
        if getattr(self, "quantized", False):
            # bitsandbytes modules cannot move back to CPU. Keep only the
            # quantized weights resident and release temporary CUDA buffers.
            torch.cuda.empty_cache()
            return
        if self.device != "cpu":
            self.logger.info("Releasing GPU %s, moving model back to CPU.", self.device)
            self.model = self.model.to("cpu")
            self.device = "cpu"
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


    def _valid_word(self, word: str, language: str) -> bool:
        """Removes stopwords and non-character words"""
        return word.isalpha() and word.lower() not in self.stopwords[language]

    def _sentence_filter_reason(
        self,
        sentence: str,
        tagged_sentence: List[Tuple[str, str, bool]],
        cfg=None,
    ) -> str:
        """Return the reason a sentence is unsuitable for question candidates."""
        cfg = cfg or filter_settings()
        normalized = " ".join(sentence.split())
        if not normalized:
            return "empty"
        if cfg["structural"]:
            if BIBLIOGRAPHIC_IDENTIFIER_REGEX.search(normalized):
                return "bibliographic_identifier"
            if CITATION_ONLY_REGEX.fullmatch(normalized):
                return "citation_only"
            if CAPTION_REGEX.match(normalized):
                return "caption"
            if TOC_REGEX.search(normalized):
                return "table_of_contents"
        if cfg["boilerplate"] and BOILERPLATE_REGEX.search(normalized):
            return "boilerplate"

        max_tokens = cfg.get("max_sentence_tokens", 0)
        if max_tokens > 0:
            tokenizer = getattr(self, "tokenizer", None)
            if tokenizer is not None:
                token_count = len(tokenizer.encode(normalized, add_special_tokens=False))
            else:
                # Fallback for tests without a loaded model. ~4 chars per MT5
                # subword token is a conservative estimate: it undercounts
                # occasional long words but tracks the true count within ~30%.
                token_count = len(normalized) // 4
            if token_count > max_tokens:
                return "sentence_too_long"

        visible_chars = [char for char in normalized if not char.isspace()]
        non_letters = sum(not char.isalpha() for char in visible_chars)
        if (cfg["non_letter_ratio"] < 1.0 and visible_chars
                and non_letters / len(visible_chars) > cfg["non_letter_ratio"]):
            return "mostly_numeric_or_symbolic"

        if not cfg["weak_text"]:
            return None

        alphabetic_tokens = [word for word, _, _ in tagged_sentence if word.isalpha()]
        content_tokens = [
            word for word, tag, is_valid in tagged_sentence
            if is_valid and tag in {"NOUN", "VERB", "ADJ", "ADV"}
        ]
        weak_score = 0
        if len(alphabetic_tokens) < cfg["min_alpha_tokens"]:
            weak_score += 1
        if len(content_tokens) < cfg["min_content_tokens"]:
            weak_score += 1
        if alphabetic_tokens and len(content_tokens) / len(alphabetic_tokens) < cfg["min_content_ratio"]:
            weak_score += 1
        if LIST_MARKER_REGEX.match(normalized) or normalized[-1] not in ".?!":
            weak_score += 1
        if weak_score >= cfg["weak_score"]:
            return "low_information"
        return None

    def _tag_sent(self, tokenized_sentence: List[str], language: str) -> str:
        """
        Returns word class of the given word -> takes also sentence to consider context.
        The reason is that for english language, the procedure is less obvious from only the word.
        """
        if(language == 'sk'):
            tagged = self.tagger_slovak.tag([tokenized_sentence])
            tagged = list(map(lambda token: (token.word, translations_word_class[token.tag[0]]) if token.tag[0] in translations_word_class else (token.word, "X"), tagged))
        elif(language == 'cs'):
            tagged = self.tagger_czech.tag([tokenized_sentence])
            tagged = list(map(lambda token: (token.word, translations_word_class[token.tag[0]]) if token.tag[0] in translations_word_class else (token.word, "X"), tagged))

        elif(language == 'en'):
            tagged = nltk.tag.pos_tag(tokenized_sentence, tagset="universal")
        return tagged




    @torch.inference_mode()
    def _model_guess_word(self, words: List[AnalyzedWord], max_range: int, num_of_words: int) -> List[str]:
        """
        Returns a list of "max_range" most probable words with their corresponding scores for each element in the batch 
        """
        blank_sentences = [re.sub('<<BLANK>>', '<extra_id_0>', word.blanked_sentence) for word in words]
        tokens = self.tokenizer(blank_sentences, max_length=512, padding=True, truncation=True, return_tensors="pt")
        if not self.use_openvino:
            tokens = tokens.to(self.device)
        out = self.model.generate(**tokens,
                        logits_processor=transformers.LogitsProcessorList([GenerateNWordsLogitsProcessor(num_of_words=num_of_words, 
                                                                                                         eos_token_id=250098, 
                                                                                                         suppress_tokens=self.suppress_tokens)]),
                        return_dict_in_generate=True, 
                        output_scores=True, 
                        num_beams=max_range,
                        max_new_tokens=20, 
                        num_return_sequences=max_range,
                        eos_token_id=250098) # 250098 is equivalent to token <extra_id_1>, the tokenizer just refuses to convert it correctly

        results = self.tokenizer.batch_decode(out.sequences, skip_special_tokens=True)
        guessed_words = [[re.sub(r'\s*<extra_id_[0-9]+>\s*', '', word) for word in results]]
        return np.reshape(guessed_words, (len(words), max_range)).tolist(), \
               torch.reshape(out.sequences_scores, (len(words), max_range)).tolist()

    def _get_ml_pos(self, word: str, guessed_words: List[str], min_range: int, max_range: int) -> int:
        """Checks minrange-maxrange guessed words from the model, if it contains the given word returns its position"""
        for i in range(min_range):
            if word.lower() == guessed_words[i].lower():
                return -1
        for i in range(min_range, max(len(guessed_words), max_range)):
            if(word.lower() == guessed_words[i].lower()):
                return i
        return -1
    
    
    def _plausible_words(self, words: List[AnalyzedWord], min_range: int, max_range: int, num_of_words: int) -> List[int]:
        """Evaluates plausible words in batch, returns list of positions in the model predictions"""
        try:
            return self._plausible_words_once(
                words, min_range, max_range, num_of_words)
        except torch.cuda.OutOfMemoryError:
            if len(words) <= 1:
                raise
            split = len(words) // 2
            self.logger.warning(
                "CUDA OOM for batch size %d; retrying as %d + %d",
                len(words), split, len(words) - split,
            )
            torch.cuda.empty_cache()
            left_pos, left_predictions = self._plausible_words(
                words[:split], min_range, max_range, num_of_words)
            right_pos, right_predictions = self._plausible_words(
                words[split:], min_range, max_range, num_of_words)
            return left_pos + right_pos, left_predictions + right_predictions

    def _plausible_words_once(self, words, min_range, max_range, num_of_words):
        ml_pos_list = []
        predictions_list = []
        guessed_words_batch, scores_batch = self._model_guess_word(words, max_range, num_of_words=num_of_words)
        for guessed_words, scores, word in zip(guessed_words_batch, scores_batch, words):
            ml_pos = self._get_ml_pos(word.word, guessed_words, min_range, max_range)
            ml_pos_list.append(ml_pos)
            predictions = [{"word": word, "probability": score} for word, score in (zip(guessed_words, scores))]
            predictions_list.append(predictions)
            
        return ml_pos_list, predictions_list
        
    def _already_found_word(self, word: str, sentence: str, blanked_sentence: str, found_words: List[str], found_sentences: List[str]) -> bool:
        """Checks whether the word (subword) or sentence has already been found"""
        if(sentence in found_sentences):
            return True

        word_coef = 0.75 if len(word) == 4 else 0.6
        possible_subwords_word = [word[i:i + floor(len(word) * word_coef) ].lower() for i in range(0, len(word) - floor(len(word) * word_coef) + 1)]

        if any(subword_word in blanked_sentence.lower() for subword_word in possible_subwords_word):
                return True

        for found in found_words:
            if(len(found) < 4 and len(word) < 4):
                if(found.lower() == word.lower()):
                    return True
                else:
                    continue

            found_coef = 0.75 if len(found) == 4 else 0.6
            possible_subwords_found = [found[i:i + floor(len(found) * found_coef) ].lower() for i in range(0, len(found) - floor(len(found) * found_coef) + 1)]

            if any(subword_found in subword_word or subword_word in subword_found for subword_found in possible_subwords_found for subword_word in possible_subwords_word):
                return True

        return False

    def sentence_extract_ngrams(self, sentence: str, tagged_sentence: List[Tuple[str, str, bool]], pick_classes: List[str],
                                language: str, paragraph_idx: int, sentence_idx: int, n=1, filter_direct_citation=True,
                                match_mode: str = "all", allowed_pos_tags: List[str] = None) -> List[AnalyzedWord]:
        """Extract n-gram candidates whose POS tags satisfy `pick_classes`.

        match_mode controls how `pick_classes` (a list of POS tags) is matched
        against an n-gram's tags:
          - "all" (default): every token's tag must be in `pick_classes`
            (whitelist). E.g. ["NOUN","ADV","ADJ"] keeps n-grams made only of
            nouns/adverbs/adjectives.
          - "contains": `pick_classes` must be a subset of the n-gram's tags,
            i.e. the n-gram must contain every listed tag at least once. E.g.
            ["ADJ"] with n=3 keeps trigrams that contain an adjective.
        When `allowed_pos_tags` is supplied, every n-gram tag must also be in
        that whitelist.
        Note: in both modes every token must also be a valid word (alphabetic,
        non-stopword), so n-grams containing punctuation/stopwords are skipped.
        """
        if filter_direct_citation:
            direct_citations = [
                match.span()
                for regex in (DIRECT_CITATION_REGEX, CITATION_SPAN_REGEX)
                for match in regex.finditer(sentence)
            ]
            direct_citations.sort()

        index = 0
        ngrams = []
        direct_citation_idx = 0
        for i in range(len(tagged_sentence)-n+1):
            slice_end = min(len(tagged_sentence), i+n)
            slice = tagged_sentence[i:slice_end]
            ngram_candidates = re.search("\s?".join([re.escape(word) for word,_,_ in slice]), sentence)
            if ngram_candidates is None:
                # No match means the word tokens were separated by something other than empty string or a whitespace 
                continue
            ngram = ngram_candidates[0]
            ngram_index = (sentence[index:].find(ngram)) + index
            index = ngram_index + len(slice[0][0]) # Move the index by one token
            if not all([is_valid for _,_,is_valid in slice]):
                continue
            
            if filter_direct_citation and direct_citation_idx < len(direct_citations):
                start, end = direct_citations[direct_citation_idx]
                if index >= start and index <= end:
                    continue
                if index >= end:
                    direct_citation_idx += 1

            ngram_tags = [tag for _, tag, _ in slice]
            if match_mode == "contains":
                # pick_classes must be a subset of the n-gram's real tags.
                selected = set(pick_classes).issubset(ngram_tags)
            else:
                # "all": every token's tag must be one of pick_classes.
                selected = all(tag in set(pick_classes) for tag in ngram_tags)
            if allowed_pos_tags is not None:
                selected = selected and all(tag in set(allowed_pos_tags) for tag in ngram_tags)
            if selected:
                blanked_sentence = sentence[:ngram_index] + '<<BLANK>>' + sentence[(ngram_index + len(ngram)):]
                # NB: previously this line was `[tag for tag,_,_ in slice]`,
                # which unpacked the *word* and called it `tag`, so the
                # stored value was the surface form, not the POS tags.
                ngram_tag_str = " ".join(ngram_tags)
                ngrams.append(AnalyzedWord(ngram, ngram_tag_str, language, sentence, blanked_sentence, paragraph_idx, sentence_idx, i))
        return ngrams
    

    def scan_document(self, paragraphs: List[str], language: str, pick_classes: Dict[str, List[str]]) -> None:
        """Scan document and pick only words from chosen word classes, order these words by their frequency in the text"""
        self.logger.info("Scan document paragraphs=%d language=%s", len(paragraphs), language)
        # Language auto-correction. See MLmethod/language_detection.py for the
        # heuristic. We assume all paragraphs in one request share a language
        # (true in every deployed use case), so one detection over the joined
        # text is enough. Controlled by AVER_LANGUAGE_AUTO_CORRECT.
        # Default: "0" (off). Only "1" / "true" / "yes" / "on" turn it on.
        # The intended enable channel is WebAPI/.env or a systemd
        # ``Environment=`` drop-in, not per-process shell state. The result --
        # swap or no swap -- is stashed on ``self.language_correction`` so the
        # worker can put it in the API response and log it in jobs.db.
        self.language_correction = None
        auto_correct = os.getenv(
            "AVER_LANGUAGE_AUTO_CORRECT", "0").strip().lower() in ("1", "true", "yes", "on")
        if auto_correct:
            # Lazy import so legacy scripts that load this file outside the
            # MLmethod package still work.
            try:
                from MLmethod.language_detection import detect_language_for_paragraphs
            except ImportError:
                from .language_detection import detect_language_for_paragraphs  # type: ignore
            detection = detect_language_for_paragraphs(
                paragraphs, stopwords=self.stopwords, fallback=language)
            declared = language
            self.language_correction = {
                "declared": declared,
                "detected": detection.language,
                "applied": declared,          # updated below on a swap
                "auto_correct_enabled": True,
                **detection.to_log_dict(),
            }
            if detection.language and detection.language != declared:
                self.logger.warning(
                    "Language auto-correct: declared=%s but detected=%s "
                    "(scores=%s, margin=%.4f, n_tokens=%d, n_chars=%d). "
                    "Swapping to the detected language for POS tagging and "
                    "stopword filtering.",
                    declared, detection.language,
                    {k: round(v, 4) for k, v in detection.scores.items()},
                    detection.margin, detection.n_tokens, detection.n_chars)
                language = detection.language
                self.language_correction["applied"] = language
            elif detection.language is None:
                self.logger.info(
                    "Language auto-correct: not enough signal "
                    "(n_tokens=%d, n_chars=%d); keeping declared=%s.",
                    detection.n_tokens, detection.n_chars, declared)
            else:
                self.logger.info(
                    "Language auto-correct: declared=%s matches detected "
                    "(margin=%.4f).", declared, detection.margin)
        else:
            self.language_correction = {
                "declared": language, "detected": None, "applied": language,
                "auto_correct_enabled": False,
            }

        self.picked_words = {} # Clear memory from previous scan
        self.picked_classes = pick_classes
        self.unigrams = []
        self.bigrams = []
        self.trigrams = []
        cfg = filter_settings()
        self.logger.info("Text filter settings: %s", cfg)
        indexed_sentences = []
        rejection_counts = {}
        in_references = False

        def _preview(text: str, limit: int = 300) -> str:
            """Single-line preview capped at `limit` chars for the log."""
            normalized = " ".join(text.split())
            return normalized if len(normalized) <= limit \
                else normalized[:limit] + f"... [+{len(normalized) - limit} chars]"

        def _log_rejection(reason: str, sentence: str, paragraph_idx, sentence_idx=None):
            """Log a filter rejection so we can audit false positives.

            `sentence_too_long` is logged at INFO because it is meant to be
            rare and the operator explicitly wants to inspect every example
            (to tune AVER_FILTER_MAX_SENTENCE_TOKENS). All other reasons are
            logged at DEBUG so a normal document does not drown the log with
            hundreds of `low_information` lines. Enable DEBUG via
            AVER_LOG_LEVEL=DEBUG when investigating.
            """
            level = logging.INFO if reason == "sentence_too_long" else logging.DEBUG
            if not self.logger.isEnabledFor(level):
                return
            location = f"p{paragraph_idx}" if sentence_idx is None else f"p{paragraph_idx}s{sentence_idx}"
            self.logger.log(level, "Rejected [%s] at %s: %s",
                            reason, location, _preview(sentence))

        for paragraph_idx, paragraph in enumerate(paragraphs):
            normalized_paragraph = " ".join(paragraph.split())
            if cfg["references"] and in_references:
                rejection_counts["reference_section"] = rejection_counts.get("reference_section", 0) + 1
                _log_rejection("reference_section", paragraph, paragraph_idx)
                continue
            if cfg["references"] and REFERENCES_HEADING_REGEX.fullmatch(normalized_paragraph):
                in_references = True
                rejection_counts["references_heading"] = rejection_counts.get("references_heading", 0) + 1
                _log_rejection("references_heading", paragraph, paragraph_idx)
                continue
            sentences_in_paragraph = nltk.sent_tokenize(paragraph)
            for sentence_idx, sentence in enumerate(sentences_in_paragraph):
                indexed_sentences.append((paragraph_idx, sentence_idx, sentence))

        for cloze_class in pick_classes:
            self.picked_words[cloze_class] = []

        for paragraph_idx, sentence_idx, sentence in indexed_sentences:
            tokenized_sentence = nltk.tokenize.word_tokenize(sentence)
            tagged = [(word, tag, self._valid_word(word, language)) for word, tag in self._tag_sent(tokenized_sentence, language)]
            filter_reason = self._sentence_filter_reason(sentence, tagged, cfg)
            if filter_reason is not None:
                rejection_counts[filter_reason] = rejection_counts.get(filter_reason, 0) + 1
                _log_rejection(filter_reason, sentence, paragraph_idx, sentence_idx)
                continue
            for cloze_class in pick_classes:
                ngrams = self.sentence_extract_ngrams(sentence, tagged,
                                                      pick_classes[cloze_class]["pos_tags"],
                                                      language, paragraph_idx,
                                                      sentence_idx, n=pick_classes[cloze_class]["num_of_words"],
                                                      filter_direct_citation=cfg["citations"],
                                                      match_mode=pick_classes[cloze_class].get("match", "all"),
                                                      allowed_pos_tags=pick_classes[cloze_class].get("allowed_pos_tags"))
                for ngram in ngrams:
                    ngram.cloze_class = cloze_class
                self.picked_words[cloze_class].extend(ngrams)

        for cloze_class in pick_classes:
            self.logger.info("Cloze class %s candidates: %d", cloze_class, len(self.picked_words[cloze_class]))
        if rejection_counts:
            self.logger.info("Text filter rejections: %s", rejection_counts)

    
        # Order by the frequency of words -> more frequent comes first, due to analysis these have higher chance of being plausible
        for cloze_class in pick_classes:
            self.picked_words[cloze_class] = sorted(self.picked_words[cloze_class],
                                                   key=lambda x:(-[o.word for o in self.picked_words[cloze_class]].count(x.word)))


    def get_plausible_words(self, min_range: int, max_range: int, classes_num: Dict[str, int], batch_size=5, progress_cb=None, decision_cb=None) -> List[AnalyzedWord]:
        """Get plausible words -> right word class, model guessed this word in position from 'min_range' to 'max_range'

        If provided, progress_cb(done, total) is called after each processed
        batch. 'total' is an upper bound on the number of inference batches
        (the loop may finish earlier once enough words are found).

        If provided, decision_cb(word, decision, ml_pos, predictions) is called
        once per candidate that the language model was actually invoked on.
        Values for ``decision``:
            * ``"accept"``      -- LM matched and the candidate was returned.
            * ``"spare"``       -- LM matched but this class was already full;
              may still be used later in the cross-category deficit fill.
            * ``"reject"``      -- LM did not match (ml_pos == -1).
            * ``"reject_dup"``  -- LM matched but the surface / sentence was
              already covered by an earlier pick; the candidate is discarded.
        The callback receives the full LM top-K in ``predictions`` even on
        reject, so downstream tooling can train a pre-filter. Candidates that
        never reach the LM (early break, phase C random fallback, duplicate
        suppression before inference) are not reported here.
        """
        self.logger.info("Get plausible words min=%d max=%d batch=%d", min_range, max_range, batch_size)
        found_plausible = []
        found_words = []
        found_sentences = []
        progress_done = 0
        # Only LM-verified classes run inference batches; classes with
        # "LM_verify": False accept candidates directly and contribute no batches.
        progress_total = sum((len(self.picked_words[c]) + batch_size - 1) // batch_size
                             for c in classes_num
                             if classes_num[c] > 0
                             and self.picked_classes[c].get("LM_verify", True))

        def report():
            if progress_cb is not None:
                progress_cb(progress_done, progress_total)

        def not_dup(word):
            return not self._already_found_word(word.word, word.sentence, word.blanked_sentence,
                                                found_words, found_sentences)

        def accept(word, ml_pos, predictions):
            word.ml_pos = ml_pos
            if predictions is not None:
                word.predictions = predictions
            found_plausible.append(word)
            found_sentences.append(word.sentence)
            found_words.append(word.word)

        report()

        # Cross-category reserves, used only if some category ends up short:
        #   spare_plausible: words already confirmed plausible (free, beyond a quota)
        #   spare_untested: candidates we never ran the model on (early break)
        spare_plausible = []
        spare_untested = {}

        # ---- Phase 1: fill each category up to its quota (early break preserved) ----
        for cloze_class in classes_num:
            want = classes_num[cloze_class]
            num_of_words = self.picked_classes[cloze_class]["num_of_words"]
            candidates = self.picked_words[cloze_class]
            picked = 0

            if want <= 0:
                # Nothing requested: keep all candidates available to other categories.
                spare_untested.setdefault(cloze_class, []).extend(candidates)
                self.logger.info("Cloze class %s selected: 0 (no quota)", cloze_class)
                continue

            # ---- "LM_verify": False -> skip the language-model plausibility check ----
            # Accept the top (most frequent) candidates directly, no inference.
            # Useful for bigrams/trigrams, which the LM almost never reproduces
            # verbatim and so are filtered out under verification.
            if not self.picked_classes[cloze_class].get("LM_verify", True):
                accepted = []
                for word in candidates:
                    if picked >= want:
                        break
                    if not_dup(word):
                        accept(word, ml_pos=0, predictions=[])
                        accepted.append(word.word)
                        picked += 1
                self.logger.info("Cloze class %s: accepted %d/%d without LM verification (of %d candidates)",
                                 cloze_class, picked, want, len(candidates))
                self.logger.debug("[%s] accepted: %s", cloze_class, accepted)
                continue

            n_batches = (len(candidates) + batch_size - 1) // batch_size
            self.logger.info("Cloze class %s: LM-verifying up to %d candidates in %d batches (want %d)",
                             cloze_class, len(candidates), n_batches, want)

            # Work a mutable queue so we can defer over-represented words. Each
            # occurrence is still LM-verified at most once. Once a word has been
            # verified DUP_DEFER_AFTER times without being picked, the whole
            # remaining contiguous run of that word is moved DUP_DEFER_DISTANCE
            # places further back (clamped to the queue end), so rarer words get
            # a turn first. In the worst case every candidate is still processed.
            queue = deque(candidates)
            fail_count = {}      # lowercased word -> times LM-verified, not picked
            deferrals = 0
            batch_idx = 0
            stopped = False
            while queue and not stopped:
                # ---- Defer leading runs of words that already failed enough ----
                # `deferred_here` breaks the cycle when every leading word has
                # already been deferred this round (e.g. the queue is nothing but
                # over-failed duplicates): we then fall through and test them.
                deferred_here = set()
                while queue:
                    front_key = queue[0].word.lower()
                    if fail_count.get(front_key, 0) < DUP_DEFER_AFTER:
                        break
                    if front_key in deferred_here:
                        break
                    run = []
                    while queue and queue[0].word.lower() == front_key:
                        run.append(queue.popleft())
                    if not queue:
                        # Nothing else to try; process this run rather than loop.
                        queue.extend(run)
                        break
                    insert_at = min(DUP_DEFER_DISTANCE, len(queue))
                    for w in reversed(run):
                        queue.insert(insert_at, w)
                    deferred_here.add(front_key)
                    deferrals += len(run)

                # ---- Assemble one inference batch from the queue front ----
                batch = []
                while queue and len(batch) < batch_size:
                    word = queue.popleft()
                    if not not_dup(word):
                        continue                      # covered already; drop it
                    batch.append(word)
                if not batch:
                    break

                batch_idx += 1
                progress_done += 1
                report()
                ml_pos_list, predictions_list = self._plausible_words(batch, min_range, max_range, num_of_words)
                accepted = []
                for ml_pos, predictions, word in zip(ml_pos_list, predictions_list, batch):
                    matched = ml_pos != -1
                    self.logger.debug("[%s] cand=%r -> %s | guesses: %s", cloze_class, word.word,
                                      ("MATCH @rank %d" % ml_pos) if matched else "no match",
                                      [p["word"] for p in predictions[:6]])
                    if matched and not_dup(word):
                        if picked < want:
                            accept(word, ml_pos, predictions)
                            accepted.append(word.word)
                            picked += 1
                            if decision_cb is not None:
                                decision_cb(word, "accept", ml_pos, predictions)
                        else:
                            # Already inferred and plausible but beyond this quota:
                            # keep it for other categories instead of discarding.
                            word.ml_pos = ml_pos
                            word.predictions = predictions
                            spare_plausible.append(word)
                            if decision_cb is not None:
                                decision_cb(word, "spare", ml_pos, predictions)
                    else:
                        # Count the failure so repeated copies of a filler word
                        # get deferred once the threshold is reached.
                        key = word.word.lower()
                        fail_count[key] = fail_count.get(key, 0) + 1
                        if decision_cb is not None:
                            # Distinguish "LM said no" from "LM said yes but
                            # this surface / sentence was already picked".
                            decision_cb(word,
                                        "reject_dup" if matched else "reject",
                                        ml_pos, predictions)
                    if picked >= want:
                        stopped = True
                self.logger.debug("[%s] batch %d picked %d/%d | tested %s | accepted %s",
                                  cloze_class, batch_idx, picked, want,
                                  [w.word for w in batch], accepted)
            if stopped and queue:
                # Quota met; leftover candidates stay untested for other categories.
                spare_untested.setdefault(cloze_class, []).extend(queue)
            self.logger.info("Cloze class %s plausible picked: %d / %d (deferrals: %d)",
                             cloze_class, picked, want, deferrals)

        total_want = sum(max(0, v) for v in classes_num.values())
        deficit = total_want - len(found_plausible)

        # ---- Phase 2: only if short overall, fill from OTHER categories before random ----
        # Step A: reuse plausible words already found (for free) in other categories.
        if deficit > 0 and spare_plausible:
            self.logger.info("Deficit %d: filling from %d spare plausible word(s) (cross-category).",
                             deficit, len(spare_plausible))
            for word in spare_plausible:
                if deficit <= 0:
                    break
                if not_dup(word):
                    accept(word, word.ml_pos, word.predictions)
                    deficit -= 1

        # Step B: run the model on untested leftover candidates from other categories.
        if deficit > 0 and spare_untested:
            self.logger.info("Deficit %d: testing leftover candidates from other categories.", deficit)
            for cloze_class, candidates in spare_untested.items():
                if deficit <= 0:
                    break
                num_of_words = self.picked_classes[cloze_class]["num_of_words"]
                for batch_start in range(0, len(candidates), batch_size):
                    if deficit <= 0:
                        break
                    words = candidates[batch_start:batch_start + batch_size]
                    progress_done += 1
                    report()
                    filtered_words = [w for w in words if not_dup(w)]
                    if not filtered_words:
                        continue
                    ml_pos_list, predictions_list = self._plausible_words(filtered_words, min_range, max_range, num_of_words)
                    for ml_pos, predictions, word in zip(ml_pos_list, predictions_list, filtered_words):
                        if deficit <= 0:
                            break
                        matched = ml_pos != -1
                        if matched and not_dup(word):
                            accept(word, ml_pos, predictions)
                            deficit -= 1
                            if decision_cb is not None:
                                decision_cb(word, "accept", ml_pos, predictions)
                        elif decision_cb is not None:
                            decision_cb(word,
                                        "reject_dup" if matched else "reject",
                                        ml_pos, predictions)

        # Step C: last resort, random fill from any remaining candidate (any category).
        # Words added here are flagged with random_fallback=True so the API layer
        # can surface them under a synthetic "random" category. The original
        # cloze_class is preserved on the object so downstream callers still see
        # which class the candidate came from.
        if deficit > 0:
            self.logger.info("Deficit %d: random fill (last resort).", deficit)
            all_candidates = [w for c in classes_num for w in self.picked_words[c]]
            random.shuffle(all_candidates)
            for word in all_candidates:
                if deficit <= 0:
                    break
                if not_dup(word):
                    word.ml_pos = -1
                    word.random_fallback = True
                    found_plausible.append(word)
                    found_sentences.append(word.sentence)
                    found_words.append(word.word)
                    deficit -= 1

        self.logger.info("Total plausible words returned: %d", len(found_plausible))
        return found_plausible

    @staticmethod
    def batch(iterable, n=1):
        length = len(iterable)
        for batch_start_idx in range(0, length, n):
            yield iterable[batch_start_idx:min(batch_start_idx + n, length)]


    @staticmethod
    def get_suppress_tokens(tokenizer):
        # Get all non-alphanumeric (with '-) except pad and extra_id
        suppress_tokens = []
        for id in range(tokenizer.vocab_size):
            #re.search(r"([@_!#$%^&*()<>?/\|}{~:])", tokenizer.convert_ids_to_tokens(id)) and
            if not re.search(r"<pad>", tokenizer.convert_ids_to_tokens(id)) and \
            not re.search(r"^\w+$", tokenizer.convert_ids_to_tokens(id), re.UNICODE) and \
            not re.search(r"extra_id", tokenizer.convert_ids_to_tokens(id)):
                suppress_tokens.append(id)
        return suppress_tokens
        
    """
EXAMPLE OF USAGE

f = open("./thesis.txt", "r", encoding="utf8")
text = f.read().splitlines()

analyzer = DocumentAnalyzer()
analyzer.scan_document(text, 'en', ['NOUN'])
  
plausible_words = analyzer.get_plausible_words(1, 11, {'NOUN': 10})
for found in plausible_words:
    print(found.word, found.blanked_sentence)
    
"""
