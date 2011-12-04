import logging
import datetime
import operator

from django.conf import settings
from django.utils.datastructures import SortedDict
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import ugettext as _

import askbot
import askbot.conf
from askbot.models.tag import Tag
from askbot.models.base import AnonymousContent
from askbot.models.post import PostRevision
from askbot.models.base import BaseQuerySetManager
from askbot.models import content
from askbot.models import signals
from askbot import const
from askbot.utils.lists import LazyList
from askbot.utils.slug import slugify
from askbot.utils import mysql

#todo: too bad keys are duplicated see const sort methods
QUESTION_ORDER_BY_MAP = {
    'age-desc': '-added_at',
    'age-asc': 'added_at',
    'activity-desc': '-thread__last_activity_at',
    'activity-asc': 'thread__last_activity_at',
    'answers-desc': '-thread__answer_count',
    'answers-asc': 'thread__answer_count',
    'votes-desc': '-score',
    'votes-asc': 'score',
    'relevance-desc': None#this is a special case for postges only
}

def get_tag_summary_from_questions(questions):
    """returns a humanized string containing up to 
    five most frequently used
    unique tags coming from the ``questions``.
    Variable ``questions`` is an iterable of 
    :class:`~askbot.models.Question` model objects.

    This is not implemented yet as a query set method,
    because it is used on a list.
    """
    #todo: in python 2.6 there is collections.Counter() thing
    #which would be very useful here
    tag_counts = dict()
    for question in questions:
        tag_names = question.get_tag_names()
        for tag_name in tag_names:
            if tag_name in tag_counts:
                tag_counts[tag_name] += 1
            else:
                tag_counts[tag_name] = 1
    tag_list = tag_counts.keys()
    #sort in descending order
    tag_list.sort(lambda x, y: cmp(tag_counts[y], tag_counts[x]))

    #note that double quote placement is important here
    if len(tag_list) == 1:
        last_topic = '"'
    elif len(tag_list) <= 5:
        last_topic = _('" and "%s"') % tag_list.pop()
    else:
        tag_list = tag_list[:5]
        last_topic = _('" and more')

    return '"' + '", "'.join(tag_list) + last_topic


class QuestionQuerySet(models.query.QuerySet):
    """Custom query set subclass for :class:`~askbot.models.Question`
    """
    #todo: becomes thread query set
    def create_new(
                self,
                title = None,
                author = None,
                added_at = None,
                wiki = False,
                is_anonymous = False,
                tagnames = None,
                text = None
            ):
        #todo: some work from this method will go to thread
        #and some - merged with the Answer.objects.create_new

        question = Question(
            thread=Thread.objects.create(tagnames=tagnames, last_activity_at=added_at, last_activity_by=author),
            title = title,
            author = author,
            added_at = added_at,
            wiki = wiki,
            is_anonymous = is_anonymous,
            #html field is denormalized in .save() call
            text = text,
            #summary field is denormalized in .save() call
        )
        if question.wiki:
            #DATED COMMENT
            #todo: this is confusing - last_edited_at field
            #is used as an indicator whether question has been edited
            #but in principle, post creation should count as edit as well
            question.last_edited_by = question.author
            question.last_edited_at = added_at
            question.wikified_at = added_at

        question.parse_and_save(author = author)
        question.thread.update_tags(tagnames = tagnames, user = author, timestamp = added_at)

        question.add_revision(
            author = author,
            is_anonymous = is_anonymous,
            text = text,
            comment = const.POST_STATUS['default_version'],
            revised_at = added_at,
        )
        return question

    def get_by_text_query(self, search_query):
        """returns a query set of questions, 
        matching the full text query
        """
        #todo - goes to thread - we search whole threads
        if getattr(settings, 'USE_SPHINX_SEARCH', False):
            matching_questions = Question.sphinx_search.query(search_query)
            question_ids = [q.id for q in matching_questions] 
            return Question.objects.filter(deleted = False, id__in = question_ids)
        if settings.DATABASE_ENGINE == 'mysql' and mysql.supports_full_text_search():
            return self.filter( 
                        models.Q(title__search = search_query) \
                       | models.Q(text__search = search_query) \
                       | models.Q(thread__tagnames__search = search_query) \
                       | models.Q(answers__text__search = search_query)
                    )
        elif 'postgresql_psycopg2' in askbot.get_database_engine_name():
            rank_clause = "ts_rank(question.text_search_vector, plainto_tsquery(%s))";
            search_query = '&'.join(search_query.split())
            extra_params = (search_query,)
            extra_kwargs = {
                'select': {'relevance': rank_clause},
                'where': ['text_search_vector @@ plainto_tsquery(%s)'],
                'params': extra_params,
                'select_params': extra_params,
            }
            return self.extra(**extra_kwargs)
        else:
            #fallback to dumb title match search
            return self.extra(
                        where=['title like %s'], 
                        params=['%' + search_query + '%']
                    )

    def run_advanced_search(
                        self,
                        request_user = None,
                        search_state = None
                    ):
        """all parameters are guaranteed to be clean
        however may not relate to database - in that case
        a relvant filter will be silently dropped
        """
        #todo: same as for get_by_text_query - goes to Tread
        scope_selector = getattr(
                            search_state,
                            'scope',
                            const.DEFAULT_POST_SCOPE
                        )

        search_query = search_state.query
        tag_selector = search_state.tags
        author_selector = search_state.author

        sort_method = getattr(
                            search_state, 
                            'sort',
                            const.DEFAULT_POST_SORT_METHOD
                        )

        qs = self.filter(deleted=False)#todo - add a possibility to see deleted questions

        #return metadata
        meta_data = {}
        if search_query:
            if search_state.stripped_query:
                qs = qs.get_by_text_query(search_state.stripped_query)
                #a patch for postgres search sort method
                if askbot.conf.should_show_sort_by_relevance():
                    if sort_method == 'relevance-desc':
                        qs = qs.extra(order_by = ['-relevance',])
            if search_state.query_title:
                qs = qs.filter(title__icontains = search_state.query_title)
            if len(search_state.query_tags) > 0:
                qs = qs.filter(tags__name__in = search_state.query_tags)
            if len(search_state.query_users) > 0:
                query_users = list()
                for username in search_state.query_users:
                    try:
                        user = User.objects.get(username__iexact = username)
                        query_users.append(user)
                    except User.DoesNotExist:
                        pass
                if len(query_users) > 0:
                    qs = qs.filter(author__in = query_users)

        if tag_selector: 
            for tag in tag_selector:
                qs = qs.filter(tags__name = tag)


        #have to import this at run time, otherwise there
        #a circular import dependency...
        from askbot.conf import settings as askbot_settings
        if scope_selector:
            if scope_selector == 'unanswered':
                qs = qs.filter(thread__closed = False)#do not show closed questions in unanswered section
                if askbot_settings.UNANSWERED_QUESTION_MEANING == 'NO_ANSWERS':
                    qs = qs.filter(thread__answer_count=0)#todo: expand for different meanings of this
                elif askbot_settings.UNANSWERED_QUESTION_MEANING == 'NO_ACCEPTED_ANSWERS':
                    qs = qs.filter(thread__accepted_answer__isnull=True) #answer_accepted=False
                elif askbot_settings.UNANSWERED_QUESTION_MEANING == 'NO_UPVOTED_ANSWERS':
                    raise NotImplementedError()
                else:
                    raise Exception('UNANSWERED_QUESTION_MEANING setting is wrong')
            elif scope_selector == 'favorite':
                favorite_filter = models.Q(thread__favorited_by = request_user)
                if 'followit' in settings.INSTALLED_APPS:
                    followed_users = request_user.get_followed_users()
                    favorite_filter |= models.Q(author__in = followed_users)
                    favorite_filter |= models.Q(answers__author__in = followed_users)
                qs = qs.filter(favorite_filter)
            
        #user contributed questions & answers
        if author_selector:
            try:
                #todo maybe support selection by multiple authors
                u = User.objects.get(id=int(author_selector))
                qs = qs.filter(
                            models.Q(author=u, deleted=False) \
                            | models.Q(answers__author=u, answers__deleted=False)
                        )
                meta_data['author_name'] = u.username
            except User.DoesNotExist:
                meta_data['author_name'] = None

        #get users tag filters
        ignored_tag_names = None
        if request_user and request_user.is_authenticated():
            uid_str = str(request_user.id)
            #mark questions tagged with interesting tags
            #a kind of fancy annotation, would be nice to avoid it
            interesting_tags = Tag.objects.filter(
                                    user_selections__user=request_user,
                                    user_selections__reason='good'
                                )
            ignored_tags = Tag.objects.filter(
                                    user_selections__user=request_user,
                                    user_selections__reason='bad'
                                )

            meta_data['interesting_tag_names'] = [tag.name for tag in interesting_tags]

            ignored_tag_names = [tag.name for tag in ignored_tags]
            meta_data['ignored_tag_names'] = ignored_tag_names

            if interesting_tags or request_user.has_interesting_wildcard_tags():
                #expensive query
                if request_user.display_tag_filter_strategy == \
                        const.INCLUDE_INTERESTING:
                    #filter by interesting tags only
                    interesting_tag_filter = models.Q(tags__in = interesting_tags)
                    if request_user.has_interesting_wildcard_tags():
                        interesting_wildcards = request_user.interesting_tags.split() 
                        extra_interesting_tags = Tag.objects.get_by_wildcards(
                                                            interesting_wildcards
                                                        )
                        interesting_tag_filter |= models.Q(tags__in = extra_interesting_tags)

                    qs = qs.filter(interesting_tag_filter)
                else:
                    #simply annotate interesting questions
                    qs = qs.extra(
                        select = SortedDict([
                            (
                                'interesting_score', 
                                'SELECT COUNT(1) FROM askbot_markedtag, question_tags '
                                 + 'WHERE askbot_markedtag.user_id = %s '
                                 + 'AND askbot_markedtag.tag_id = question_tags.tag_id '
                                 + 'AND askbot_markedtag.reason = \'good\' '
                                 + 'AND question_tags.question_id = question.id'
                            ),
                                ]),
                        select_params = (uid_str,),
                     )
            # get the list of interesting and ignored tags (interesting_tag_names, ignored_tag_names) = (None, None)

            if ignored_tags or request_user.has_ignored_wildcard_tags():
                if request_user.display_tag_filter_strategy == const.EXCLUDE_IGNORED:
                    #exclude ignored tags if the user wants to
                    qs = qs.exclude(tags__in=ignored_tags)
                    if request_user.has_ignored_wildcard_tags():
                        ignored_wildcards = request_user.ignored_tags.split() 
                        extra_ignored_tags = Tag.objects.get_by_wildcards(
                                                            ignored_wildcards
                                                        )
                        qs = qs.exclude(tags__in = extra_ignored_tags)
                else:
                    #annotate questions tagged with ignored tags
                    #expensive query
                    qs = qs.extra(
                        select = SortedDict([
                            (
                                'ignored_score', 
                                'SELECT COUNT(1) '
                                  + 'FROM askbot_markedtag, question_tags '
                                  + 'WHERE askbot_markedtag.user_id = %s '
                                  + 'AND askbot_markedtag.tag_id = question_tags.tag_id '
                                  + 'AND askbot_markedtag.reason = \'bad\' '
                                  + 'AND question_tags.question_id = question.id'
                            )
                                ]),
                        select_params = (uid_str, )
                     )

        if sort_method != 'relevance-desc':
            #relevance sort is set in the extra statement
            #only for postgresql
            orderby = QUESTION_ORDER_BY_MAP[sort_method]
            qs = qs.order_by(orderby)

        qs = qs.distinct()
        qs = qs.select_related(
                        'thread__last_activity_by__id',
                        'thread__last_activity_by__username',
                        'thread__last_activity_by__reputation',
                        'thread__last_activity_by__gold',
                        'thread__last_activity_by__silver',
                        'thread__last_activity_by__bronze',
                        'thread__last_activity_by__country',
                        'thread__last_activity_by__show_country',
                    )

        related_tags = Tag.objects.get_related_to_search(
                                        questions = qs,
                                        search_state = search_state,
                                        ignored_tag_names = ignored_tag_names
                                    )
        if askbot_settings.USE_WILDCARD_TAGS == True \
            and request_user.is_authenticated() == True:
            tagnames = request_user.interesting_tags
            meta_data['interesting_tag_names'].extend(tagnames.split())
            tagnames = request_user.ignored_tags
            meta_data['ignored_tag_names'].extend(tagnames.split())
        return qs, meta_data, related_tags

    def added_between(self, start, end):
        """questions added between ``start`` and ``end`` timestamps"""
        #todo: goes to thread
        return self.filter(
            added_at__gt = start
        ).exclude(
            added_at__gt = end
        )

    def get_questions_needing_reminder(self,
                                    user = None,
                                    activity_type = None,
                                    recurrence_delay = None):
        """returns list of questions that need a reminder,
        corresponding the given ``activity_type``
        ``user`` - is the user receiving the reminder
        ``recurrence_delay`` - interval between sending the
        reminders about the same question
        """
        #todo: goes to thread
        from askbot.models import Activity#avoid circular import
        question_list = list()
        for question in self:
            try:
                activity = Activity.objects.get(
                    user = user,
                    question = question,
                    activity_type = activity_type
                )
                now = datetime.datetime.now()
                if now < activity.active_at + recurrence_delay:
                    continue
            except Activity.DoesNotExist:
                activity = Activity(
                    user = user,
                    question = question,
                    activity_type = activity_type,
                    content_object = question,
                )
            activity.active_at = datetime.datetime.now()
            activity.save()
            question_list.append(question)
        return question_list

    #todo: this function is similar to get_response_receivers
    #profile this function against the other one
    #todo: maybe this must be a query set method, not manager method
    def get_question_and_answer_contributors(self, question_list):
        """returns query set of Thread contributors
        """
        #todo: goes to thread - queries will be simplified too
        answer_list = []
        #question_list = list(question_list)#important for MySQL, b/c it does not support
        from askbot.models.answer import Answer
        q_id = [question.id for question in question_list]
        a_id = list(Answer.objects.filter(question__in=q_id).values_list('id', flat=True))
        u_id = set(self.filter(id__in=q_id).values_list('author', flat=True))
        u_id = u_id.union(
                    set(Answer.objects.filter(id__in=a_id).values_list('author', flat=True))
                )

        #todo: this does not belong gere - here we select users with real faces
        #first and limit the number of users in the result for display
        #on the main page, we might also want to completely hide fake gravatars
        #and show only real images and the visitors - even if he does not have
        #a real image and try to prompt him/her to upload a picture
        from askbot.conf import settings as askbot_settings
        avatar_limit = askbot_settings.SIDEBAR_MAIN_AVATAR_LIMIT
        contributors = User.objects.filter(id__in=u_id).order_by('avatar_type', '?')[:avatar_limit]
        #print contributors
        #could not optimize this query with indices so it was split into what's now above
        #contributors = User.objects.filter(
        #                            models.Q(questions__in=question_list) \
        #                            | models.Q(answers__in=answer_list)
        #                           ).distinct()
        #contributors = list(contributors)
        return contributors

    def get_author_list(self, **kwargs):
        #todo: - this is duplication - answer manager also has this method
        #will be gone when models are consolidated
        #note that method get_question_and_answer_contributors is similar in function
        #todo: goes to thread
        authors = set()
        for question in self:
            authors.update(question.get_author_list(**kwargs))
        return list(authors)


class QuestionManager(BaseQuerySetManager):
    """chainable custom query set manager for 
    questions
    """
    #todo: becomes thread manager
    def get_query_set(self):
        return QuestionQuerySet(self.model)

class Thread(models.Model):
    # Denormalised data, transplanted from Question
    tagnames = models.CharField(max_length=125)
    view_count = models.PositiveIntegerField(default=0)
    favourite_count = models.PositiveIntegerField(default=0)
    answer_count = models.PositiveIntegerField(default=0)
    last_activity_at = models.DateTimeField(default=datetime.datetime.now)
    last_activity_by = models.ForeignKey(User, related_name='unused_last_active_in_threads')

    followed_by     = models.ManyToManyField(User, related_name='followed_threads')
    favorited_by    = models.ManyToManyField(User, through='FavoriteQuestion', related_name='unused_favorite_threads')

    closed          = models.BooleanField(default=False)
    closed_by       = models.ForeignKey(User, null=True, blank=True) #, related_name='closed_questions')
    closed_at       = models.DateTimeField(null=True, blank=True)
    close_reason    = models.SmallIntegerField(
                                            choices=const.CLOSE_REASONS,
                                            null=True,
                                            blank=True
                                        )

    accepted_answer = models.ForeignKey('askbot.Answer', null=True, blank=True)
    answer_accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'askbot'

    def _question(self):
        return Question.objects.get(thread=self)

    def update_favorite_count(self):
        self.favourite_count = FavoriteQuestion.objects.filter(thread=self).count()
        self.save()

    def update_answer_count(self):
        self.answer_count = self.get_answers().count()
        self.save()

    def increase_view_count(self, increment=1):
        qset = Thread.objects.filter(id=self.id)
        qset.update(view_count=models.F('view_count') + increment)
        self.view_count = qset.values('view_count')[0]['view_count'] # get the new view_count back because other pieces of code relies on such behaviour

    def set_closed_status(self, closed, closed_by, closed_at, close_reason):
        self.closed = closed
        self.closed_by = closed_by
        self.closed_at = closed_at
        self.close_reason = close_reason
        self.save()

    def set_accepted_answer(self, answer, timestamp):
        if answer and answer.question.thread != self:
            raise ValueError("Answer doesn't belong to this thread")
        self.accepted_answer = answer
        self.answer_accepted_at = timestamp
        self.save()

    def set_last_activity(self, last_activity_at, last_activity_by):
        self.last_activity_at = last_activity_at
        self.last_activity_by = last_activity_by
        self.save()

    def get_answers(self, user=None):
        """returns query set for answers to this question
        that may be shown to the given user
        """
        thread_question = self._question()

        if user is None or user.is_anonymous():
            return thread_question.answers.filter(deleted=False)
        else:
            if user.is_administrator() or user.is_moderator():
                return thread_question.answers.all()
            else:
                return thread_question.answers.filter(
                                models.Q(deleted = False) | models.Q(author = user) \
                                | models.Q(deleted_by = user)
                            )


    def get_similarity(self, other_question = None):
        """return number of tags in the other question
        that overlap with the current question (self)
        """
        my_tags = set(self._question().get_tag_names())
        others_tags = set(other_question.get_tag_names())
        return len(my_tags & others_tags)

    def get_similar_questions(self):
        """
        Get 10 similar questions for given one.
        Questions with the individual tags will be added to list if above questions are not full.

        This function has a limitation that it will
        retrieve only 100 records then select 10 most similar
        from that list as querying entire database may
        be very expensive - this function will benefit from
        some sort of optimization
        """

        def get_data():
            thread_question = self._question()

            tags_list = thread_question.tags.all()
            similar_questions = Question.objects.filter(tags__in=tags_list).\
                                    exclude(id = self.id).exclude(deleted = True).distinct()[:100]

            similar_questions = list(similar_questions)
            for question in similar_questions:
                question.similarity = self.get_similarity(other_question=question)

            similar_questions.sort(key=operator.attrgetter('similarity'), reverse=True)
            return similar_questions[:10]

        return LazyList(get_data)

    def remove_author_anonymity(self):
        """removes anonymous flag from the question
        and all its revisions
        the function calls update method to make sure that
        signals are not called
        """
        #note: see note for the is_anonymous field
        #it is important that update method is called - not save,
        #because we do not want the signals to fire here
        thread_question = self._question()
        Question.objects.filter(id=thread_question.id).update(is_anonymous=False)
        thread_question.revisions.all().update(is_anonymous=False)

    def update_tags(self, tagnames = None, user = None, timestamp = None):
        """
        Updates Tag associations for a question to match the given
        tagname string.

        When tags are removed and their use count hits 0 - the tag is
        automatically deleted.

        When an added tag does not exist - it is created

        Tag use counts are recalculated

        A signal tags updated is sent
        """
        thread_question = self._question()

        previous_tags = list(thread_question.tags.all())

        previous_tagnames = set([tag.name for tag in previous_tags])
        updated_tagnames = set(t for t in tagnames.split(' '))

        removed_tagnames = previous_tagnames - updated_tagnames
        added_tagnames = updated_tagnames - previous_tagnames

        modified_tags = list()
        #remove tags from the question's tags many2many relation
        if removed_tagnames:
            removed_tags = [tag for tag in previous_tags if tag.name in removed_tagnames]
            thread_question.tags.remove(*removed_tags)

            #if any of the removed tags reached use count == 1 that means they must be deleted
            for tag in removed_tags:
                if tag.used_count == 1:
                    #we won't modify used count b/c it's done below anyway
                    removed_tags.remove(tag)
                    #todo - do we need to use fields deleted_by and deleted_at?
                    tag.delete()#auto-delete tags whose use count dwindled

            #remember modified tags, we'll need to update use counts on them
            modified_tags = removed_tags

        #add new tags to the relation
        if added_tagnames:
            #find reused tags
            reused_tags = Tag.objects.filter(name__in = added_tagnames)
            #undelete them, because we are using them
            reused_count = reused_tags.update(
                                    deleted = False,
                                    deleted_by = None,
                                    deleted_at = None
                                )
            #if there are brand new tags, create them and finalize the added tag list
            if reused_count < len(added_tagnames):
                added_tags = list(reused_tags)

                reused_tagnames = set([tag.name for tag in reused_tags])
                new_tagnames = added_tagnames - reused_tagnames
                for name in new_tagnames:
                    new_tag = Tag.objects.create(
                                            name = name,
                                            created_by = user,
                                            used_count = 1
                                        )
                    added_tags.append(new_tag)
            else:
                added_tags = reused_tags

            #finally add tags to the relation and extend the modified list
            thread_question.tags.add(*added_tags)
            modified_tags.extend(added_tags)

        #if there are any modified tags, update their use counts
        if modified_tags:
            Tag.objects.update_use_counts(modified_tags)
            signals.tags_updated.send(None,
                                question = thread_question,
                                tags = modified_tags,
                                user = user,
                                timestamp = timestamp
                            )
            return True

        return False

    def retag(self, retagged_by=None, retagged_at=None, tagnames=None, silent=False):
        if None in (retagged_by, retagged_at, tagnames):
            raise Exception('arguments retagged_at, retagged_by and tagnames are required')

        thread_question = self._question()

        self.tagnames = tagnames
        self.save()

        # Update the Question itself
        if silent == False:
            thread_question.last_edited_at = retagged_at
            #thread_question.thread.last_activity_at = retagged_at
            thread_question.last_edited_by = retagged_by
            #thread_question.thread.last_activity_by = retagged_by
        thread_question.save()

        # Update the Question's tag associations
        self.update_tags(tagnames=tagnames, user=retagged_by, timestamp=retagged_at)

        # Create a new revision
        latest_revision = thread_question.get_latest_revision()
        PostRevision.objects.create_question_revision(
            question   = thread_question,
            title      = latest_revision.title,
            author     = retagged_by,
            revised_at = retagged_at,
            tagnames   = tagnames,
            summary    = const.POST_STATUS['retagged'],
            text       = latest_revision.text
        )

    def has_favorite_by_user(self, user):
        if not user.is_authenticated():
            return False

        return FavoriteQuestion.objects.filter(thread=self, user=user).exists()

    def get_last_update_info(self):
        thread_question = self._question()

        when, who = thread_question.post_get_last_update_info()

        answers = thread_question.answers.all()
        for a in answers:
            a_when, a_who = a.post_get_last_update_info()
            if a_when > when:
                when = a_when
                who = a_who

        return when, who


class Question(content.Content):
    post_type = 'question'
    thread = models.ForeignKey('Thread', unique=True, related_name='questions')

    #todo: this really becomes thread,
    title    = models.CharField(max_length=300)
    tags     = models.ManyToManyField('Tag', related_name='questions')

    #note: anonymity here applies to question only, but
    #the field will still go to thread
    #maybe we should rename it to is_question_anonymous
    #we might have to duplicate the is_anonymous on the Post,
    #if we are to allow anonymous answers
    #the reason is that the title and tags belong to thread,
    #but the question body to Post
    is_anonymous = models.BooleanField(default=False) 

    objects = QuestionManager()

    class Meta(content.Content.Meta):
        db_table = u'question'

    def _get_slug(self):
        return slugify(self.title)

    slug = property(_get_slug)

if getattr(settings, 'USE_SPHINX_SEARCH', False):
    from djangosphinx.models import SphinxSearch
    Question.add_to_class(
        'sphinx_search',
        SphinxSearch(
            index = settings.ASKBOT_SPHINX_SEARCH_INDEX,
            mode = 'SPH_MATCH_ALL'
        )
    )


        
class QuestionView(models.Model):
    question = models.ForeignKey(Question, related_name='viewed')
    who = models.ForeignKey(User, related_name='question_views')
    when = models.DateTimeField()

    class Meta:
        app_label = 'askbot'

class FavoriteQuestion(models.Model):
    """A favorite Question of a User."""
    thread        = models.ForeignKey(Thread)
    user          = models.ForeignKey(User, related_name='unused_user_favorite_questions')
    added_at      = models.DateTimeField(default=datetime.datetime.now)

    class Meta:
        app_label = 'askbot'
        db_table = u'favorite_question'
    def __unicode__(self):
        return '[%s] favorited at %s' %(self.user, self.added_at)


class AnonymousQuestion(AnonymousContent):
    """question that was asked before logging in
    maybe the name is a little misleading, the user still
    may or may not want to stay anonymous after the question
    is published
    """
    title = models.CharField(max_length=300)
    tagnames = models.CharField(max_length=125)
    is_anonymous = models.BooleanField(default=False)

    def publish(self,user):
        added_at = datetime.datetime.now()
        Question.objects.create_new(
                                title = self.title,
                                added_at = added_at,
                                author = user,
                                wiki = self.wiki,
                                is_anonymous = self.is_anonymous,
                                tagnames = self.tagnames,
                                text = self.text,
                                )
        self.delete()
