'''
    实体链接
    link
    返回列表：[dict({info_dict, mention, 抽取方式, 分值})]
'''
import re

import jieba
from fuzzywuzzy import fuzz, process

from ..BertEntityRelationClassification.BertERClsPredict import \
    predict as BertERCls
from ..common import LTP, AsyncNeoDriver
from ..config import config
from .LinkUtil import recognize_entity
from ..common.HITBert import cosine_word_similarity


def contain_chinese(s):
    s = s.replace('-', '').lower()
    if s in {'wifi', 'atm', 'vip', 'kfc'}:
        return True
    for c in s:
        if ('\u4e00' <= c <= '\u9fa5'):
            return True
    return False


def contain_english(s):
    return bool(re.search('[A-Za-z]', s))


class RuleLinker():
    def __init__(self, driver=None):
        if driver is None:
            self.driver = AsyncNeoDriver.get_driver(name='default')
        else:
            self.driver = driver
        self.load_all_entities()

    def load_all_entities(self, entity_labels=['Instance', 'SubGenre', 'Genre']):
        all_entities = []
        for entity_label in entity_labels:
            tmp_entities = self.driver.get_all_entities(entity_label).result()
            if entity_label == 'Genre':
                tmp_entities = list(filter(
                    lambda x: '类' not in x['name'] and '航空公司' not in x['name'] and '行李安检' not in x['name'], tmp_entities))
            for e in tmp_entities:
                e['label'] = entity_label
            all_entities += tmp_entities
        self.id2ent = {x['neoId']: x for x in all_entities}
        self.ent_names = {x['name'] for x in all_entities}

    def link(self, sent, limits=None):
        # use bert embedding to fuzzy match entities
        mention_list = recognize_entity(sent)
        if mention_list == []:
            return []
        self.sent_cut = LTP.customed_jieba_cut(sent, cut_stop=True)
        # print('cut:', self.cut)
        res = []
        for mention in mention_list:
            one_res = []
            if self.filter_q_entity(mention):
                continue
            converted_item = self.convert_abstract_verb(
                mention, sent, limits)
            for ent in self.id2ent.values():
                # for ent_name in self.ent_names:
                ent_name = ent['name']
                # 该实体为英文而问的有汉语或相反
                if contain_chinese(converted_item) and not contain_chinese(ent_name) or contain_english(
                        converted_item) and not contain_english(ent_name):
                    continue
                filtered_key = self.filter_key(ent_name)
                if filtered_key == '':
                    continue
                score = cosine_word_similarity(converted_item, filtered_key)
                score1 = fuzz.token_sort_ratio(
                    ' '.join(converted_item), ' '.join(filtered_key))
                score2 = fuzz.token_sort_ratio(
                    converted_item, filtered_key)
                if score1 > 70 and score2 >= 50 \
                   and len(converted_item) > 1:
                    score *= 1.2
                    score += (score1-70)/100
                '''if item in key and len(item) > 1:
                    score *= 1.1'''
                # punish english words
                '''if not is_contain_chinese(key) or len(key) == 1:
                    score *= 0.8'''
                one_res.append({
                    'ent': ent,
                    'mention': mention,
                    'id': ent['neoId'],
                    'score': score,
                    'source': 'rule'
                })
            one_res.sort(key=lambda x: x['score'], reverse=True)
            for a_res in one_res[:3]:
                if a_res['score'] > config.simi_ths:
                    res.append(a_res)
        res.sort(key=lambda x: x['score'], reverse=True)
        return res

    def convert_abstract_verb(self, word, sent, limits):
        convert_dict = config.ABSTRACT_DICT
        if word in convert_dict:
            # TODO ** 和词典严重耦合的硬编码 **
            if word == '换':
                if limits is not None and limits['币种'] != '' or '货币' in sent or '外币' in sent:
                    return convert_dict.get(word)[0]
                elif '尿' in sent:
                    return convert_dict.get(word)[1]
                else:
                    return word
            # if wd == '换':
            #     if '货币' or '外币'
            return convert_dict[word]
        else:
            # 去除"服务"字段的影响
            return word.replace('服务', '')

    def filter_q_entity(self, item):
        for wd in config.airport.filter_words:
            if wd in item:
                return True
        for wd in config.airport.remove_words:
            if wd == item:
                return True
        # or not is_contain_chinese(item):
        if '时间' in item or '地点' in item or '位置' in item or '地方' in item or '收费' in item or '价格' in item or '限制' in item or item == '电话':
            return True
        return False

    def filter_key(self, item):
        item = item.split('(')[0].split('（')[0].lower()
        if '服务' in item and item != '服务':
            item = item.replace('服务', '')
        if item == '柜台' or item == '其他柜台' or item == '行李' or item == '咨询':
            item = ''
        return item


class BertLinker():
    def __init__(self, driver=None):
        if driver is None:
            self.driver = AsyncNeoDriver.get_driver(name='default')
        else:
            self.driver = driver

    def link(self, sent):
        _, _, ent_type_top3 = BertERCls(sent)
        # print(ent_type_top3)
        instances_top3 = [self.driver.get_instance_of_genre(ent_type)
                          for ent_type in ent_type_top3]
        res = []
        for rank, instances in enumerate(instances_top3):
            for e in instances:
                ent = {
                    'ent': e,
                    'id': e['neoId'],
                    'rank': rank+1,
                    'source': 'bert',
                    'score': 1/(rank+1)
                }
                res.append(ent)
        return res


class CommercialLinker():
    def __init__(self, driver=None):
        if driver is None:
            self.driver = AsyncNeoDriver.get_driver(name='default')
        else:
            self.driver = driver
        self.content2entId = self.build_revert_index()

    def link(self, sent):
        content_keys = self.retrieve_content_keys(sent)
        res = []
        for content, score in content_keys:
            ent_ids = self.content2entId[content]
            for ent_id in ent_ids:
                e = self.driver.get_entity_by_id(ent_id).result()[0]
                ent = {
                    'ent': e,
                    'id': ent_id,
                    'score': score/100,
                    'source': 'commercial',
                    'content': content
                }
                res.append(ent)
        return res

    def build_revert_index(self):
        entities = self.driver.get_entities_by_genre('Instance').result()
        # entities += self.driver.get_entities_by_genre('SubGenre').result()
        content2entId = {}
        for ent in entities:
            ent_id = ent['neoId']
            content_str = ent.get('服务内容', '')
            content = content_str.split(';')
            for c in content:
                if c == '':
                    continue
                if c in content2entId:
                    content2entId[c].append(ent_id)
                else:
                    content2entId[c] = [ent_id]
        return content2entId

    def retrieve_content_keys(self, sent):
        words = LTP.customed_jieba_cut(sent)
        sent = ''.join(words)
        res = process.extract(sent, self.content2entId.keys(),
                              scorer=fuzz.UQRatio,
                              limit=2)

        return res


def test_rule_linker():
    from ..qa.Limiter import Limiter
    questions = ['打火机可以携带吗？', '机场有婴儿车可以租用吗', '机场有轮椅可以租用吗',
                 '停车场怎么收费', '停车场费用怎么样？', '停车场一个小时多少钱？', '停车场多少钱？']
    rule_linker = RuleLinker()
    for q in questions:
        limits = Limiter(q).check()
        res = rule_linker.link(q, limits)
        print(q)
        print(res)


def test_bert_linker():
    bert_linker = BertLinker()
    sent = '有可以玩游戏的地方吗？'
    # sent = '东航的值机柜台在哪？'
    res = bert_linker.link(sent)
    print(res)


def test_commercial_linker():
    commercial_linker = CommercialLinker()
    # print(commercial_linker.content2entId)
    sent = '有可以打游戏的地方吗？'
    # sent = '东航的值机柜台在哪？'
    res = commercial_linker.link(sent)
    print(res)


if __name__ == '__main__':
    # test_bert_linker()
    # test_commercial_linker()
    test_rule_linker()
