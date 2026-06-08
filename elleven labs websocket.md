# Realtime

GET /v1/speech-to-text/realtime

Realtime speech-to-text transcription service. This WebSocket API enables streaming audio input and receiving transcription results.

## Event Flow
- Audio chunks are sent as `input_audio_chunk` messages
- Transcription results are streamed back in various formats (partial, committed, with timestamps)
- Supports manual commit or VAD-based automatic commit strategies

Authentication is done either by providing a valid API key in the `xi-api-key` header or by providing a valid token in the `token` query parameter. Tokens can be generated from the [single use token endpoint](/docs/api-reference/tokens/create). Use tokens if you want to transcribe audio from the client side.


Reference: https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime

## AsyncAPI Specification

```yaml
asyncapi: 2.6.0
info:
  title: V 1 Speech To Text Realtime
  version: subpackage_v1SpeechToTextRealtime.v1SpeechToTextRealtime
  description: >
    Realtime speech-to-text transcription service. This WebSocket API enables
    streaming audio input and receiving transcription results.


    ## Event Flow

    - Audio chunks are sent as `input_audio_chunk` messages

    - Transcription results are streamed back in various formats (partial,
    committed, with timestamps)

    - Supports manual commit or VAD-based automatic commit strategies


    Authentication is done either by providing a valid API key in the
    `xi-api-key` header or by providing a valid token in the `token` query
    parameter. Tokens can be generated from the [single use token
    endpoint](/docs/api-reference/tokens/create). Use tokens if you want to
    transcribe audio from the client side.
channels:
  /v1/speech-to-text/realtime:
    description: >
      Realtime speech-to-text transcription service. This WebSocket API enables
      streaming audio input and receiving transcription results.


      ## Event Flow

      - Audio chunks are sent as `input_audio_chunk` messages

      - Transcription results are streamed back in various formats (partial,
      committed, with timestamps)

      - Supports manual commit or VAD-based automatic commit strategies


      Authentication is done either by providing a valid API key in the
      `xi-api-key` header or by providing a valid token in the `token` query
      parameter. Tokens can be generated from the [single use token
      endpoint](/docs/api-reference/tokens/create). Use tokens if you want to
      transcribe audio from the client side.
    bindings:
      ws:
        query:
          type: object
          properties:
            model_id:
              type: string
            token:
              type: string
            include_timestamps:
              type: boolean
              default: false
            include_language_detection:
              type: boolean
              default: false
            audio_format:
              $ref: '#/components/schemas//v1/speech-to-text/realtime_audio_format'
              default: pcm_16000
            language_code:
              type: string
            commit_strategy:
              $ref: '#/components/schemas//v1/speech-to-text/realtime_commit_strategy'
              default: manual
            keyterms:
              type: array
              items:
                type: string
            no_verbatim:
              type: boolean
              default: false
            vad_silence_threshold_secs:
              type: number
              format: double
              default: 1.5
            vad_threshold:
              type: number
              format: double
              default: 0.4
            min_speech_duration_ms:
              type: integer
              default: 100
            min_silence_duration_ms:
              type: integer
              default: 100
            enable_logging:
              type: boolean
              default: true
        headers:
          type: object
          properties:
            xi-api-key:
              type: string
    publish:
      operationId: v-1-speech-to-text-realtime-publish
      summary: subscribe
      description: Receive transcription results from the WebSocket
      message:
        name: subscribe
        title: subscribe
        description: Receive transcription results from the WebSocket
        payload:
          $ref: '#/components/schemas/V1SpeechToTextRealtimeSubscribe'
    subscribe:
      operationId: v-1-speech-to-text-realtime-subscribe
      summary: publish
      description: Send audio data to the WebSocket
      message:
        name: publish
        title: publish
        description: Send audio data to the WebSocket
        payload:
          $ref: '#/components/schemas/V1SpeechToTextRealtimePublish'
servers:
  Production:
    url: wss://api.elevenlabs.io/
    protocol: wss
    x-default: true
  Production US:
    url: wss://api.us.elevenlabs.io/
    protocol: wss
  Production EU:
    url: wss://api.eu.residency.elevenlabs.io/
    protocol: wss
  Production India:
    url: wss://api.in.residency.elevenlabs.io/
    protocol: wss
components:
  schemas:
    /v1/speech-to-text/realtime_audio_format:
      type: string
      enum:
        - pcm_8000
        - pcm_16000
        - pcm_22050
        - pcm_24000
        - pcm_44100
        - pcm_48000
        - ulaw_8000
      default: pcm_16000
      description: Audio encoding format for speech-to-text.
      title: /v1/speech-to-text/realtime_audio_format
    /v1/speech-to-text/realtime_commit_strategy:
      type: string
      enum:
        - manual
        - vad
      default: manual
      description: Strategy for committing transcriptions.
      title: /v1/speech-to-text/realtime_commit_strategy
    AudioFormatEnum:
      type: string
      enum:
        - pcm_8000
        - pcm_16000
        - pcm_22050
        - pcm_24000
        - pcm_44100
        - pcm_48000
        - ulaw_8000
      default: pcm_16000
      description: Audio encoding format for speech-to-text.
      title: AudioFormatEnum
    MessagesSessionStartedConfigCommitStrategy:
      type: string
      enum:
        - manual
        - vad
      description: Strategy for committing transcriptions.
      title: MessagesSessionStartedConfigCommitStrategy
    MessagesSessionStartedConfig:
      type: object
      properties:
        sample_rate:
          type: integer
          description: Sample rate of the audio in Hz.
        audio_format:
          $ref: '#/components/schemas/AudioFormatEnum'
          default: pcm_16000
        language_code:
          type: string
          description: Language code in ISO 639-1 or ISO 639-3 format.
        commit_strategy:
          $ref: '#/components/schemas/MessagesSessionStartedConfigCommitStrategy'
          description: Strategy for committing transcriptions.
        vad_silence_threshold_secs:
          type: number
          format: double
          description: Silence threshold in seconds.
        vad_threshold:
          type: number
          format: double
          description: Threshold for voice activity detection.
        min_speech_duration_ms:
          type: integer
          description: Minimum speech duration in milliseconds.
        min_silence_duration_ms:
          type: integer
          description: Minimum silence duration in milliseconds.
        model_id:
          type: string
          description: ID of the model to use for transcription.
        enable_logging:
          type: boolean
          description: >-
            When enable_logging is set to false zero retention mode will be used
            for the request. This will mean history features are unavailable for
            this request. Zero retention mode may only be used by enterprise
            customers.
        include_timestamps:
          type: boolean
          description: >-
            Whether the session will include word-level timestamps in the
            committed transcript.
        include_language_detection:
          type: boolean
          description: >-
            Whether the session will include language detection in the committed
            transcript.
        keyterms:
          type: array
          items:
            type: string
          description: List of keyterms the model is biased towards.
        no_verbatim:
          type: boolean
          description: >-
            Whether filler words and disfluencies are removed from the
            transcript.
      description: Configuration for the transcription session.
      title: MessagesSessionStartedConfig
    SessionStarted:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - session_started
          description: The message type identifier.
        session_id:
          type: string
          description: Unique identifier for the session.
        config:
          $ref: '#/components/schemas/MessagesSessionStartedConfig'
          description: Configuration for the transcription session.
      required:
        - message_type
        - session_id
        - config
      description: Payload sent when the transcription session is successfully started.
      title: SessionStarted
    PartialTranscript:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - partial_transcript
          description: The message type identifier.
        text:
          type: string
          description: Partial transcription text.
      required:
        - message_type
        - text
      description: Payload for partial transcription results that may change.
      title: PartialTranscript
    CommittedTranscript:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - committed_transcript
          description: The message type identifier.
        text:
          type: string
          description: Committed transcription text.
      required:
        - message_type
        - text
      description: Payload for committed transcription results.
      title: CommittedTranscript
    TranscriptionWordType:
      type: string
      enum:
        - word
        - spacing
      description: The type of word.
      title: TranscriptionWordType
    TranscriptionWord:
      type: object
      properties:
        text:
          type: string
          description: The transcribed word.
        start:
          type: number
          format: double
          description: Start time in seconds.
        end:
          type: number
          format: double
          description: End time in seconds.
        type:
          $ref: '#/components/schemas/TranscriptionWordType'
          description: The type of word.
        speaker_id:
          type: string
          description: The ID of the speaker if available.
        logprob:
          type: number
          format: double
          description: Confidence score for this word.
        characters:
          type: array
          items:
            type: string
          description: The characters in the word.
      description: Word-level transcription data with timing information.
      title: TranscriptionWord
    CommittedTranscriptWithTimestamps:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - committed_transcript_with_timestamps
          description: The message type identifier.
        text:
          type: string
          description: Committed transcription text.
        language_code:
          type:
            - string
            - 'null'
          description: Detected or specified language code.
        words:
          type:
            - array
            - 'null'
          items:
            $ref: '#/components/schemas/TranscriptionWord'
          description: Word-level information with timestamps.
      required:
        - message_type
        - text
      description: Payload for committed transcription results with word-level timestamps.
      title: CommittedTranscriptWithTimestamps
    ScribeError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - error
          description: The message type identifier.
        error:
          type: string
          description: Error message describing what went wrong.
      required:
        - message_type
        - error
      description: Payload for error events during transcription.
      title: ScribeError
    ScribeAuthError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - auth_error
          description: The message type identifier.
        error:
          type: string
          description: Authentication error details.
      required:
        - message_type
        - error
      description: Payload for authentication errors.
      title: ScribeAuthError
    ScribeQuotaExceededError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - quota_exceeded
          description: The message type identifier.
        error:
          type: string
          description: Quota exceeded error details.
      required:
        - message_type
        - error
      description: Payload for quota exceeded errors.
      title: ScribeQuotaExceededError
    ScribeThrottledError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - commit_throttled
          description: The message type identifier.
        error:
          type: string
          description: Throttled error details.
      required:
        - message_type
        - error
      description: Payload for throttled errors.
      title: ScribeThrottledError
    ScribeUnacceptedTermsError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - unaccepted_terms
          description: The message type identifier.
        error:
          type: string
          description: Unaccepted terms error details.
      required:
        - message_type
        - error
      description: Payload for unaccepted terms errors.
      title: ScribeUnacceptedTermsError
    ScribeRateLimitedError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - rate_limited
          description: The message type identifier.
        error:
          type: string
          description: Rate limited error details.
      required:
        - message_type
        - error
      description: Payload for rate limited errors.
      title: ScribeRateLimitedError
    ScribeQueueOverflowError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - queue_overflow
          description: The message type identifier.
        error:
          type: string
          description: Queue overflow error details.
      required:
        - message_type
        - error
      description: Payload for queue overflow errors.
      title: ScribeQueueOverflowError
    ScribeResourceExhaustedError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - resource_exhausted
          description: The message type identifier.
        error:
          type: string
          description: Resource exhausted error details.
      required:
        - message_type
        - error
      description: Payload for resource exhausted errors.
      title: ScribeResourceExhaustedError
    ScribeSessionTimeLimitExceededError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - session_time_limit_exceeded
          description: The message type identifier.
        error:
          type: string
          description: Session time limit exceeded error details.
      required:
        - message_type
        - error
      description: Payload for session time limit exceeded errors.
      title: ScribeSessionTimeLimitExceededError
    ScribeInputError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - input_error
          description: The message type identifier.
        error:
          type: string
          description: Input error details.
      required:
        - message_type
        - error
      description: Payload for input errors.
      title: ScribeInputError
    ScribeChunkSizeExceededError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - chunk_size_exceeded
          description: The message type identifier.
        error:
          type: string
          description: Chunk size exceeded error details.
      required:
        - message_type
        - error
      description: Payload for chunk size exceeded errors.
      title: ScribeChunkSizeExceededError
    ScribeInsufficientAudioActivityError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - insufficient_audio_activity
          description: The message type identifier.
        error:
          type: string
          description: Insufficient audio activity error details.
      required:
        - message_type
        - error
      description: Payload for insufficient audio activity errors.
      title: ScribeInsufficientAudioActivityError
    ScribeTranscriberError:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - transcriber_error
          description: The message type identifier.
        error:
          type: string
          description: Transcriber error details.
      required:
        - message_type
        - error
      description: Payload for transcriber errors.
      title: ScribeTranscriberError
    V1SpeechToTextRealtimeSubscribe:
      oneOf:
        - $ref: '#/components/schemas/SessionStarted'
        - $ref: '#/components/schemas/PartialTranscript'
        - $ref: '#/components/schemas/CommittedTranscript'
        - $ref: '#/components/schemas/CommittedTranscriptWithTimestamps'
        - $ref: '#/components/schemas/ScribeError'
        - $ref: '#/components/schemas/ScribeAuthError'
        - $ref: '#/components/schemas/ScribeQuotaExceededError'
        - $ref: '#/components/schemas/ScribeThrottledError'
        - $ref: '#/components/schemas/ScribeUnacceptedTermsError'
        - $ref: '#/components/schemas/ScribeRateLimitedError'
        - $ref: '#/components/schemas/ScribeQueueOverflowError'
        - $ref: '#/components/schemas/ScribeResourceExhaustedError'
        - $ref: '#/components/schemas/ScribeSessionTimeLimitExceededError'
        - $ref: '#/components/schemas/ScribeInputError'
        - $ref: '#/components/schemas/ScribeChunkSizeExceededError'
        - $ref: '#/components/schemas/ScribeInsufficientAudioActivityError'
        - $ref: '#/components/schemas/ScribeTranscriberError'
      title: V1SpeechToTextRealtimeSubscribe
    InputAudioChunk:
      type: object
      properties:
        message_type:
          type: string
          enum:
            - input_audio_chunk
          description: The message type identifier.
        audio_base_64:
          type: string
          format: base64
          description: Base64-encoded audio data.
        commit:
          type: boolean
          description: Whether to commit the transcription after this chunk.
        sample_rate:
          type: integer
          description: Sample rate of the audio in Hz.
        previous_text:
          type: string
          description: >-
            Send text context to the model. Can only be sent alongside the first
            audio chunk. If sent in a subsequent chunk, an error will be
            returned.
      required:
        - message_type
        - audio_base_64
        - commit
        - sample_rate
      description: Payload for sending audio chunks from client to server.
      title: InputAudioChunk
    V1SpeechToTextRealtimePublish:
      oneOf:
        - $ref: '#/components/schemas/InputAudioChunk'
      title: V1SpeechToTextRealtimePublish

```