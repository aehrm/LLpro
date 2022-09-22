from __future__ import annotations

import collections
import itertools
import json
import logging
import logging.handlers
import multiprocessing
import os.path
import re
import time
import unicodedata
from abc import abstractmethod
from typing import Iterable, Sequence, Dict, Tuple, Any, Callable, Union, List

import pandas
import regex as re


class Token:
    id: int
    doc: str
    word: str
    sentence: int
    lemma: str
    pos: str
    morph: str
    head: int
    deprel: str
    fields: Dict[Tuple[str, str], Any]

    def __init__(self, fields: Dict[Tuple[str, str], Any] = None, metadata: Dict[Tuple[str, str], Dict] = None):
        self.fields = {}
        self.metadata = {}
        if fields is not None:
            self.update_fields(fields)
        if metadata is not None:
            self.update_metadata(metadata)

    def update_fields(self, d):
        self.fields.update(d)

    def update_metadata(self, d):
        self.metadata.update(d)

    def has_field(self, field, module_name=None):
        if module_name is not None:
            return (field, module_name) in self.fields.keys()
        else:
            return any(f == field for f, _ in self.fields.keys())

    def get_field(self, field, module_name=None, default=None):
        if module_name is not None:
            if default is not None:
                return self.fields.get((field, module_name), default)
            else:
                return self.fields[(field, module_name)]

        candidates = [(field_key, field_module_name) for field_key, field_module_name in self.fields.keys() if
                      field_key == field]
        if len(candidates) > 1:
            raise TypeError(f'Field {field} set by multiple modules; call get_field(field, module_name)')
        if len(candidates) == 0:
            if default is None:
                raise TypeError(f'Field {field} not set')
            else:
                return '_'
        return self.fields[candidates[0]]

    def get_metadata(self, field, module_name):
        return self.metadata.get((field, module_name), None)

    def set_field(self, field, module_name, value, **metadata):
        self.fields[(field, module_name)] = value
        if metadata is not None and type(metadata) == dict and len(metadata.keys()) > 0:
            self.metadata[(field, module_name)] = metadata

    def __setattr__(self, field, value):
        if field in ['id', 'doc', 'word', 'sentence', 'lemma', 'pos', 'morph', 'head', 'deprel']:
            raise TypeError('Fields need to be set with set_field(field, module_name)')
        else:
            object.__setattr__(self, field, value)

    def __getattribute__(self, key):
        if key in ['id', 'doc', 'word', 'sentence', 'lemma', 'pos', 'morph', 'head', 'deprel']:
            return self.get_field(key)
        else:
            return object.__getattribute__(self, key)

    def __str__(self):
        return self.fields.__str__()

    @staticmethod
    def get_sentences(tokens: Iterable[Token]) -> Iterable[List[Token]]:
        """
        Splits tokens at sentence boundaries.
        Given an iterable of tokens, this generator yields an iterable of tokens for every sentence.
        """
        import more_itertools
        return more_itertools.split_when(tokens, lambda a, b: a.sentence != b.sentence)

    @staticmethod
    def get_documents(tokens: Iterable[Token]) -> Iterable[List[Token]]:
        """
        Splits tokens at document boundaries.
        Given an iterable of tokens, this generator yields an iterable of tokens for every document.
        """
        import more_itertools
        return more_itertools.split_when(tokens, lambda a, b: a.doc != b.doc)

    @staticmethod
    def get_chunks(tokens: Iterable[Token], max_chunk_len=None, min_chunk_len=None, sequence_length_function=None,
                   borders='sentences') -> Iterable[List[Token]]:
        if borders == 'sentences':
            chunks = list(Token.get_sentences(tokens))
        elif borders == 'tokens':
            chunks = [[tok] for tok in tokens]
        else:
            raise ValueError

        if (max_chunk_len is None) is (min_chunk_len is None):
            raise ValueError

        if sequence_length_function is None:
            len_fn = len
        else:
            len_fn = sequence_length_function

        if min_chunk_len is not None:
            process_chunk = []
            for c in chunks:
                process_chunk.extend(c)
                if len_fn(process_chunk) >= min_chunk_len:
                    yield process_chunk
                    process_chunk = []

            if len_fn(process_chunk) > 0:
                yield process_chunk
        else:
            process_chunk = []
            for c in chunks:
                c = list(c)
                if len_fn(c) > max_chunk_len:
                    if borders == 'sentences':
                        # fallback: make intra-sentence splits
                        yield process_chunk
                        for split in Token.get_chunks(c, max_chunk_len=max_chunk_len,
                                                      sequence_length_function=sequence_length_function,
                                                      borders='tokens'):
                            yield split
                        process_chunk = []
                    else:
                        raise ValueError(
                            f'Token {c[0].name} has length {len_fn(c)} > max_chunk_len = {max_chunk_len} and cannot be split!')
                elif len_fn(process_chunk + c) > max_chunk_len:
                    yield process_chunk
                    process_chunk = c
                else:
                    process_chunk.extend(c)

            if len_fn(process_chunk) > 0:
                yield process_chunk

    @staticmethod
    def to_conll(processed_tokens, **module_names) -> Iterable[str]:
        for sent in Token.get_sentences(processed_tokens):
            for tok in sent:
                misc_items = []
                for rw_type in tok.get_field('redewiedergabe', module_names.get('redewiedergabe', None), default=[]):
                    misc_items.append(f'STWR{rw_type}=yes')
                for ner in tok.get_field('ner', module_names.get('ner', None), default=[]):
                    misc_items.append(f'NER={ner}')
                for cluster_id in tok.get_field('coref_clusters', module_names.get('coref_clusters', None), default=[]):
                    misc_items.append(f'CorefID={cluster_id}')
                for frame in tok.get_field('srl', module_names.get('srl', None), default=[]):
                    if 'sense' in frame.keys():
                        misc_items.append(f'SemanticRole={frame["id"]}:{frame["sense"]}')
                    else:
                        misc_items.append(f'SemanticRole={frame["id"]}:{frame["role"]}')
                field_strings = [tok.id, tok.word,
                                 tok.get_field('lemma', module_names.get('lemmatizer', None), default='_'),
                                 '_',  # UPOS
                                 tok.get_field('pos', module_names.get('pos', None), default='_'),
                                 tok.get_field('morph', module_names.get('morph', None), default='_'),
                                 tok.get_field('head', module_names.get('head', None), default='_'),
                                 tok.get_field('deprel', module_names.get('deprel', None), default='_'),
                                 '_',  # DEPS
                                 '|'.join(misc_items)
                                 ]
                yield '\t'.join([str(x) for x in field_strings])
            yield ''

    @staticmethod
    def to_dataframe(processed_tokens):
        df = pandas.json_normalize([tok.to_dict() for tok in processed_tokens])
        df.columns = df.columns.map(lambda x: re.sub(r'^(\w+)\.(\w+)\.value', r'\1.\2', x))
        return df

    def to_dict(self):
        obj = collections.defaultdict(lambda: collections.defaultdict(dict))
        for (field, module), value in self.fields.items():
            obj[field][module]['value'] = value
        for (field, module), meta in self.metadata.items():
            obj[field][module]['metadata'] = meta
        return obj

    def to_json(self):
        return json.dumps(self.to_dict())

    @staticmethod
    def from_dict(tok_dict):
        tok = Token()
        for field, module_objs in tok_dict.items():
            for module, module_obj in module_objs.items():
                if 'value' in module_obj.keys():
                    tok.fields[(field, module)] = tok_dict[field][module]['value']
                if 'metadata' in module_obj.keys():
                    tok.metadata[(field, module)] = tok_dict[field][module]['metadata']
        return tok

    @staticmethod
    def from_json(json_str):
        return Token.from_dict(json.loads(json_str))


class Paragraph:

    def __init__(self, text: str, **meta):
        self.text = text
        self.meta = meta

    def __str__(self):
        return self.text


class Tokenizer:

    def __init__(self):
        pass

    def __str__(self):
        return self.name

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def tokenize(self, content: str, filename: str = None) -> Iterable[Token]:
        """
        Splits ``content`` into tokens. The general contract of ``tokenize`` is as follows: Implementations are
        expected to initialize tokens, and set their `word`, `doc`, `id` and `sent` field (i.e., initialize with
        ``Token({('word', self.name): tok_word, ('doc', self.name): filename, ('id', self.name): i, ('sentence',
        self.name): s})``), and finally yield these tokens.
        """
        raise NotImplementedError


class ParagraphingTokenizer(Tokenizer):

    IRREGULAR_CHARACTERS = re.compile(
        r'[^\P{dt}\p{dt=canonical}]|[^\p{Latin}\pN-"‚‘„“.?!,;:\-–—*()\[\]{}/\'«‹›»’+&%# \t\n]',
        flags=re.UNICODE | re.MULTILINE)

    def __init__(self, normalize=True, check_characters=True, paragraph_separator=None, section_pattern=None):
        super(ParagraphingTokenizer, self).__init__()
        self.normalize = normalize
        self.check_characters = check_characters
        self.paragraph_separator = paragraph_separator
        self.section_pattern = section_pattern

    def tokenize(self, content: str, filename: str = None) -> Iterable[Token]:
        if self.normalize:
            content = unicodedata.normalize('NFKC', content)

        if self.check_characters:
            irr = [unicodedata.name(x) for x in set(self.IRREGULAR_CHARACTERS.findall(content))]
            if len(irr) > 0:
                logging.warning(f'Found irregular characters in {filename}: {", ".join(irr)}')

        paragraphs = self.to_paragraphs(content)

        sentence_id = 0
        for para in paragraphs:
            for sentence in Token.get_sentences(self.tokenize_paragraph(para)):
                for tok in sentence:
                    tok.set_field('sentence', self.name, sentence_id)  # re-enumerate sentences
                    if filename is not None:
                        tok.set_field('doc', self.name, filename)
                    for k, v in para.meta.items():
                        tok.set_field(k, self.name, v)
                    yield tok
                sentence_id = sentence_id + 1

    def to_paragraphs(self, content: str) -> Iterable[Paragraph]:
        if self.paragraph_separator is None:
            yield Paragraph(content)
            return

        paragraph_strings = list(re.split(self.paragraph_separator, content, flags=re.UNICODE | re.MULTILINE))
        para_id = 0
        section_id = 0
        for para in paragraph_strings:
            if self.section_pattern and re.fullmatch(self.section_pattern, para, flags=re.UNICODE | re.MULTILINE):
                if para_id > 0:
                    section_id = section_id + 1
                continue

            yield Paragraph(para, paragraph_id=para_id, section_id=section_id)
            para_id = para_id + 1

    def tokenize_paragraph(self, para: Paragraph) -> Iterable[Token]:
        """
        Splits ``para`` into tokens. The general contract of ``tokenize_paragraph`` is as follows: Implementations are
        expected to initialize tokens, and set their `word`, `id` and `sent` field (i.e., initialize with
        ``Token({('word', self.name): tok_word, ('id', self.name): i, ('sentence', self.name): s})``),
        and finally yield these tokens.
        """
        raise NotImplementedError


class Module:

    def __str__(self):
        return self.name

    @property
    def name(self) -> str:
        return type(self).__name__

    def run(self, tokens: Sequence[Token], pbar=None, pbar_opts=None, **kwargs) -> None:
        from tqdm import tqdm
        """
        Convenience method. Runs ``self.process`` while printing progress on a tqdm progress bar.
        :param pbar: (Optional.) Use supplied progress bar instead of constructing one.
        :param pbar_opts: (Optional.) Supply additional options to constructed progress bar.
        """
        if pbar is None:
            pbar_opts = pbar_opts if pbar_opts is not None else {}
            pbar = tqdm(total=len(tokens), unit='tok', postfix=str(self), dynamic_ncols=True, **pbar_opts)

        def my_update_fn(x: int):
            pbar.update(x)

        self.before_run()
        self.process(tokens, my_update_fn, **kwargs)
        pbar.update(len(tokens) - pbar.n)
        self.after_run()
        pbar.close()

    def before_run(self):
        pass

    def after_run(self):
        pass

    @abstractmethod
    def process(self, tokens: Sequence[Token], update_fn: Callable[[int], None], **kwargs) -> None:
        """
        Main function of a module. Performs some NLP task on the supplied sequence of tokens.
        The general contract of ``process`` is:

        - Implementations are expected to modify the tokens in-place, i.e. invoke
          ``set_field(field, my_val, self.name)`` on the sequence of tokens.
        - Implementations are expected to report progress by calling ``update_fn(x)`` whenever ``x`` new tokens
          were processed.
        - Implementations can expect that the supplied sequence of tokens forms precisely one document.
        """
        raise NotImplementedError


_WORKER_MODULE: Module = None


class ParallelizedModule(Module):

    def __init__(self, module: Union[Module | Callable[[], Module]], num_processes: int,
                 borders: str = 'sentences', tokens_per_process: int = 1, name: str = None):
        """
        This module class implements a parallelization of some base module on multiple child processes. Each child
        process initializes a specified module of the same class. When processing tokens, this module offloads the
        processing to the child processes.

        :param module: The base module. Either a class object of type ``Module`` or a callable returning a ``Module`` instance.
        :param num_processes: The number of child processes.
        :param borders: (Optional. Default is ``"sentences"``.) Passed to ``Token.get_chunks``. Specifies on which
          boundaries a sequence of tokens can be split into chunks, when passing to subprocesses. When ``"sentences"``
          then each child process is processing a sequence of full sentences. When ``"tokens"`` then each child process
          is processing a sequence of tokens, possibly ending mid-sentence.
        :param tokens_per_process: Approximate number of tokens to be passed to subprocesses. If `chunking` is ``"tokens"``,
          then at most ``tokens_per_process`` are passed to each subprocess. If `chunking` is ``"sentences"``, then
          subprocesses are passed possibly more than ``tokens_per_process`` tokens, until the end of the sentence
          is reached.
        """
        if name is not None:
            self._name = name
        elif type(module) is type:
            self._name = module.__name__
        else:
            self._name = type(self).__name__

        if borders not in {'sentences', 'tokens'}:
            raise AttributeError()

        self.num_processes = num_processes
        self.borders = borders
        self.tokens_per_process = tokens_per_process
        logging.info(f"Starting {num_processes} processes of {self._name}")
        self.pool = multiprocessing.Pool(processes=num_processes, initializer=ParallelizedModule._init_worker,
                                         initargs=(module,))

    @property
    def name(self):
        return self._name

    def __str__(self):
        return self._name + 'x' + str(self.num_processes)

    def process(self, tokens: Sequence[Token], update_fn, **kwargs):
        m = multiprocessing.Manager()
        q = m.Queue()
        process_chunks = list(Token.get_chunks(tokens, min_chunk_len=self.tokens_per_process))
        for i, chunk in enumerate(process_chunks):
            self.pool.apply_async(ParallelizedModule._process_worker, (chunk, i, q))
        processed_chunks = [None] * len(process_chunks)
        while True:
            kind, i, value = q.get()
            if kind == 'update':
                update_fn(value)
            elif kind == 'result':
                processed_chunks[i] = value
                if not any(x is None for x in processed_chunks):
                    break

        for tok, modified_tok in zip(tokens, itertools.chain.from_iterable(processed_chunks)):
            tok.update_fields(modified_tok.fields)
            tok.update_metadata(modified_tok.metadata)

    @staticmethod
    def _init_worker(module_constructor):
        global _WORKER_MODULE
        _WORKER_MODULE = module_constructor()

    @staticmethod
    def _process_worker(tokens, i, out_queue):
        global _WORKER_MODULE

        def send_update(x):
            out_queue.put(('update', i, x))

        _WORKER_MODULE.process(tokens, send_update)
        out_queue.put(('result', i, tokens))
        return


def pipeline_process(tokenizer: Tokenizer, modules: Iterable[Module], filenames: Sequence[str],
                     file_pbar_opts=None, module_pbar_opts=None) -> Tuple[str, Sequence[Token]]:
    """
    Runs the specified tokenizer and modules on the specified files, displaying one progress bar for (global) file
    processing progress, and one progress bar for module progress in the currently processed file.
    After each file is processed, its filename and processed token sequence is yielded.
    """
    file_pbar_opts = file_pbar_opts if file_pbar_opts is not None else {}
    module_pbar_opts = module_pbar_opts if module_pbar_opts is not None else {}

    file_sizes = [os.path.getsize(f) for f in filenames]
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm
    with logging_redirect_tqdm():
        file_pbar = tqdm(total=sum(file_sizes), position=0, unit='B', unit_scale=True, dynamic_ncols=True,
                         **file_pbar_opts)
        file_pbar.set_description_str(f'0/{len(filenames)}')
        for i, (filename, size) in enumerate(zip(filenames, file_sizes)):
            with open(filename) as f:
                content = f.read()
            logging.info(f'Start tokenization for {filename}')
            tokens = list(tokenizer.tokenize(content, filename))
            logging.info(f'Start tagging for {filename}')
            for module in modules:
                logging.info(f'Start module {module} for {filename}')
                pbar_opts = {'position': 1, 'leave': False}
                pbar_opts.update(module_pbar_opts)
                start_time = time.time()
                module.run(tokens, pbar_opts=pbar_opts)
                end_time = time.time()
                logging.info(
                    f'Finished module {module} for {filename} ({len(tokens) / (end_time - start_time):.0f}tok/s)')

            file_pbar.update(size)
            file_pbar.set_description_str(f'{i + 1}/{len(filenames)}')
            yield filename, tokens
    file_pbar.close()
