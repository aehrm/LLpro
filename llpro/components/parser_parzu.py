import itertools
import logging
import multiprocessing
import queue
import sys

import more_itertools
from spacy import Language
from spacy.tokens import Span, Doc
from typing import List, Iterable, Callable, Tuple

from ..common import Module


@Language.factory("parser_parzu_parallelized", requires=['token.tag'], assigns=['token.dep', 'token.head'], default_config={
    'parzu_home': 'resources/ParZu', 'num_processes': 1, 'tokens_per_process': 1000, 'pbar_opts': None
})
def parser_parzu_parallelized(nlp, name, parzu_home, num_processes, tokens_per_process, pbar_opts):
    return ParzuParallelized(name=name, parzu_home=parzu_home, num_processes=num_processes, tokens_per_process=tokens_per_process, pbar_opts=pbar_opts)


class ParzuParallelized(Module):

    def __init__(self, name, parzu_home='resources/ParZu', num_processes: int = 1, tokens_per_process: int = 1000,
                 pbar_opts=None):
        super().__init__(name, pbar_opts=pbar_opts)
        self.num_processes = num_processes
        self.tokens_per_process = tokens_per_process
        logging.info(f"Starting {num_processes} processes of {name}")
        self.pool = multiprocessing.Pool(processes=num_processes, initializer=ParzuParallelized._init_worker, initargs=({'parzu_home': parzu_home},))

    def close(self):
        self.pool.close()
        self.pool.join()

    def split_doc_into_chunks(self, doc: Doc) -> Iterable[Span]:
        def sentlist_len(list_of_sents):
            return sum(len(x) for x in list_of_sents)

        for sentences in more_itertools.constrained_batches(doc.sents, max_size=self.tokens_per_process, get_len=sentlist_len):
            yield doc[sentences[0].start:sentences[-1].end]


    def process(self, doc: Doc, progress_fn: Callable[[int], None]) -> Doc:
        m = multiprocessing.Manager()
        q = m.Queue()
        process_chunks: List[Span] = list(self.split_doc_into_chunks(doc))
        results = []
        for i, span in enumerate(process_chunks):
            serialized = [[(tok.i, tok.text, tok.tag_) for tok in sent] for sent in span.sents]
            results.append(self.pool.apply_async(ParzuParallelized._process_worker, (serialized, i, q)))

        while any(not res.ready() for res in results) or not q.empty():
            try:
                kind, i, value = q.get(timeout=1)
                if kind == 'update':
                    progress_fn(value)
            except queue.Empty:
                pass

        for r in results:
            r = r.get()
            for token_result in r:
                tok = doc[token_result['index']]
                tok.dep_ = token_result['deprel']
                tok.head = doc[token_result['head']]

        return doc

    @staticmethod
    def _init_worker(worker_kwargs):
        global _WORKER_MODULE
        _WORKER_MODULE = ParzuWorker(**worker_kwargs)

    @staticmethod
    def _process_worker(serialized_span, i, out_queue):
        global _WORKER_MODULE

        def send_update(x):
            out_queue.put(('update', i, x))

        result = _WORKER_MODULE.__call__(serialized_span, send_update)
        return result

class ParzuWorker:

    def __init__(self, parzu_home='resources/ParZu'):
        sys.path.insert(0, str(parzu_home))
        from parzu_class import process_arguments, Parser

        self.opts = process_arguments(commandline=False)
        self.parser = Parser(self.opts, timeout=1000)

    def process_parzu(self, serialized_sentence: List[Tuple]):
        newinput = []
        for _, text, tag in serialized_sentence:
            newinput.append(text + '\t' + tag)

        reformatted_input = "\n".join(newinput)

        output = self.parser.main(
            reformatted_input, inputformat="tagged", outputformat="conll"
        )
        return output

    def __call__(self, serialized_span: List[List[Tuple]], update_fn: Callable[[int], None]):
        result = []
        it = more_itertools.peekable(itertools.chain(*serialized_span))

        for sentence in serialized_span:
            for processed_sent in self.process_parzu(sentence):
                index_of_first_token, _, _ = it.peek()
                for line in processed_sent.split('\n'):
                    if line.strip() == '':
                        continue
                    i, _, _ = next(it)
                    (
                        _,
                        word,
                        lemma,
                        _,
                        pos,
                        feats,
                        head,
                        deprel,
                        deps,
                        misc,
                    ) = line.strip().split("\t")
                    if deprel == 'root':
                        head = None
                    else:
                        head = int(line.split('\t')[6])

                    result.append({'index': i,
                                   'head': i if head is None else index_of_first_token + int(head) - 1,
                                   'deprel': deprel})
                    update_fn(1)

        return result