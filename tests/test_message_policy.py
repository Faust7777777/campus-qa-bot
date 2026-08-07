from luna_kb.policy import DecisionKind, InboundMessage, MessagePolicy


def test_private_messages_from_an_unlisted_sender_never_enter_the_answer_flow() -> None:
    policy = MessagePolicy(allowed_group_ids={10001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-1",
            message_type="private",
            group_id=None,
            user_id=20001,
            text="#奖学金怎么申请",
        )
    )

    assert decision.kind is DecisionKind.IGNORE
    assert decision.reason == "private_message"


def test_hash_prefix_does_not_bypass_the_group_allowlist() -> None:
    policy = MessagePolicy(allowed_group_ids={10001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-2",
            message_type="group",
            group_id=99999,
            user_id=20001,
            text="#奖学金怎么申请",
        )
    )

    assert decision.kind is DecisionKind.IGNORE
    assert decision.reason == "group_not_allowed"


def test_hash_prefix_forces_answer_flow_and_is_removed_from_question() -> None:
    policy = MessagePolicy(allowed_group_ids={10001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-3",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="  #  奖学金怎么申请  ",
        )
    )

    assert decision.kind is DecisionKind.FORCE
    assert decision.reason == "hash_prefix"
    assert decision.question == "奖学金怎么申请"


def test_empty_hash_prefix_is_ignored() -> None:
    policy = MessagePolicy(allowed_group_ids={10001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-4",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text=" #   ",
        )
    )

    assert decision.kind is DecisionKind.IGNORE
    assert decision.reason == "empty_forced_question"


def test_ordinary_question_is_sent_to_scope_classification() -> None:
    policy = MessagePolicy(allowed_group_ids={10001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-5",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="  奖学金怎么申请  ",
        )
    )

    assert decision.kind is DecisionKind.CLASSIFY
    assert decision.reason == "question_candidate"
    assert decision.question == "奖学金怎么申请"


def test_ordinary_group_noise_is_ignored_without_classification() -> None:
    policy = MessagePolicy(allowed_group_ids={10001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-6",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="哈哈哈哈",
        )
    )

    assert decision.kind is DecisionKind.IGNORE
    assert decision.reason == "obvious_noise"


def test_question_mark_does_not_promote_obvious_noise() -> None:
    policy = MessagePolicy({10001})

    for index, text in enumerate(("666?", "谢谢？", "怎么办怎么办怎么办")):
        decision = policy.decide(
            InboundMessage(
                message_id=f"noise-{index}",
                message_type="group",
                group_id=10001,
                user_id=20001,
                text=text,
            )
        )
        assert decision.kind is DecisionKind.IGNORE
        assert decision.reason == "obvious_noise"


def test_hash_prefix_still_forces_a_noise_like_question() -> None:
    policy = MessagePolicy({10001})

    decision = policy.decide(
        InboundMessage(
            message_id="forced-noise",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="#666?",
        )
    )

    assert decision.kind is DecisionKind.FORCE
    assert decision.question == "666?"
    assert decision.reason == "hash_prefix"


def test_a_listed_sender_may_ask_privately() -> None:
    # Trying the bot in a group makes every wrong answer public, so a named
    # account can ask it directly.  Naming the account is the whole control:
    # otherwise the QQ number alone is a private endpoint to the knowledge base.
    policy = MessagePolicy(allowed_group_ids={10001}, allowed_user_ids={20001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-1",
            message_type="private",
            group_id=None,
            user_id=20001,
            text="奖学金怎么申请？",
        )
    )

    assert decision.kind is DecisionKind.CLASSIFY
    assert decision.question == "奖学金怎么申请？"


def test_a_listed_sender_is_listed_for_private_chat_only() -> None:
    # Being allowed to ask privately says nothing about which groups the bot
    # answers in, or the group allowlist would leak through the back door.
    policy = MessagePolicy(allowed_group_ids={10001}, allowed_user_ids={20001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-2",
            message_type="group",
            group_id=99999,
            user_id=20001,
            text="奖学金怎么申请？",
        )
    )

    assert decision.kind is DecisionKind.IGNORE
    assert decision.reason == "group_not_allowed"


def test_noise_from_a_listed_sender_is_still_noise() -> None:
    policy = MessagePolicy(allowed_group_ids={10001}, allowed_user_ids={20001})

    decision = policy.decide(
        InboundMessage(
            message_id="m-3",
            message_type="private",
            group_id=None,
            user_id=20001,
            text="哈哈",
        )
    )

    assert decision.kind is DecisionKind.IGNORE
    assert decision.reason == "obvious_noise"
